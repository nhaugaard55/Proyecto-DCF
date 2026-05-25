import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, cast
from urllib.parse import urlencode

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from xhtml2pdf import pisa

from .models import AnalysisRecord, WatchlistGroup, WatchlistItem

from dcf_core.DCF_Main import ejecutar_dcf
from dcf_core.business_cycle import get_business_cycle_phase
from dcf_core.company_stage import detect_company_stage, STAGE_META
from dcf_core.empresa import build_filtros_por_etapa
from dcf_core.analyst_estimates import get_analyst_estimates
from dcf_core.insider_trading import get_insider_trading
from dcf_core.multi_model_valuation import run_all_models
from dcf_core.search import CompanySearchResult, search_companies


_SYMBOL_PATTERN = re.compile(r"^\s*([A-Za-z0-9.\-:]+)")

_DCF_CACHE_TTL = 600   # 10 minutos
_TYPE_CACHE_TTL = 3600  # 1 hora
_AUTO_FUENTE = "auto"

_UNSUPPORTED_QUOTE_TYPES: dict[str, str] = {
    "ETF":            "un ETF (fondo cotizado en bolsa)",
    "MUTUALFUND":     "un fondo de inversión",
    "INDEX":          "un índice bursátil",
    "FUTURE":         "un contrato de futuros",
    "CRYPTOCURRENCY": "una criptomoneda",
    "CURRENCY":       "una divisa",
    "OPTION":         "una opción financiera",
    "WARRANT":        "un warrant",
}


def _check_ticker_eligibility(ticker: str) -> str | None:
    """
    Devuelve un mensaje de error si el ticker no es una acción analizable,
    o None si es apto para el DCF. Resultado cacheado 1 hora.
    """
    cache_key = f"ticker_eligibility_{ticker}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None  # "" → None (apto)

    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        quote_type = info.get("quoteType")
        long_name = info.get("longName") or info.get("shortName")
    except Exception:
        cache.set(cache_key, "", _TYPE_CACHE_TTL)
        return None  # No bloquear si yfinance falla

    if not quote_type and not long_name:
        msg = (
            f"No se encontró ninguna empresa con el ticker \"{ticker}\". "
            "Verifica que el símbolo sea correcto."
        )
        cache.set(cache_key, msg, _TYPE_CACHE_TTL)
        return msg

    if quote_type and quote_type not in ("EQUITY",):
        tipo = _UNSUPPORTED_QUOTE_TYPES.get(quote_type, f"un instrumento de tipo {quote_type}")
        name_part = f' ("{long_name}")' if long_name else ""
        msg = (
            f"El ticker \"{ticker}\"{name_part} corresponde a {tipo}. "
            "El análisis intrínseco está disponible únicamente para acciones de empresas."
        )
        cache.set(cache_key, msg, _TYPE_CACHE_TTL)
        return msg

    cache.set(cache_key, "", _TYPE_CACHE_TTL)
    return None


def _cached_ejecutar_dcf(ticker: str) -> dict:
    """Ejecuta DCF automático con caché de 10 minutos por ticker."""
    cache_key = f"dcf_result_auto_{ticker}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    resultado = ejecutar_dcf(ticker, "auto", _AUTO_FUENTE)
    cache.set(cache_key, resultado, _DCF_CACHE_TTL)
    return resultado


def _resolver_ticker(raw_ticker: str, raw_query: str) -> str:
    """Normaliza el símbolo a partir de la selección o del texto libre."""

    ticker_limpio = (raw_ticker or "").strip().upper()
    if ticker_limpio:
        return ticker_limpio

    query_limpio = (raw_query or "").strip()
    if not query_limpio:
        return ""

    match = _SYMBOL_PATTERN.match(query_limpio)
    potencial = match.group(1).upper() if match else ""

    if potencial and " " not in potencial:
        return potencial

    coincidencias = search_companies(query_limpio, limit=1)
    if coincidencias:
        return coincidencias[0].symbol.upper()

    return potencial or query_limpio.upper()


def _to_decimal(value, *, places: int | None = 4):
    if value in (None, "", "N/D"):
        return None
    if isinstance(value, Decimal):
        dec_value = value
    else:
        try:
            dec_value = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
    if places is None:
        return dec_value
    quant = Decimal("1." + ("0" * places))
    return dec_value.quantize(quant, rounding=ROUND_HALF_UP)


def _clean_numeric(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None


def _extract_chart_series(entries):
    labels = []
    values = []
    if not entries:
        return labels, values

    for item in entries:
        if isinstance(item, dict):
            labels.append(str(item.get('anio', '')))
            values.append(_clean_numeric(item.get('valor')))
        else:
            labels.append(str(getattr(item, 'anio', '')))
            values.append(_clean_numeric(getattr(item, 'valor', None)))
    return labels, values


def _build_chart_data(resultado):
    if not resultado:
        return {
            'has_resultado': False,
            'fcf_historico_labels': [],
            'fcf_historico_series': [],
            'fcf_proyectado_labels': [],
            'fcf_proyectado_series': [],
            'revenue_historico_labels': [],
            'revenue_historico_series': [],
            'net_income_historico_labels': [],
            'net_income_historico_series': [],
            'data_fcf_labels': [],
            'data_fcf_series': [],
            'data_rev_labels': [],
            'data_rev_series': [],
            'data_ni_labels': [],
            'data_ni_series': [],
            'data_gm_labels': [],
            'data_gm_series': [],
            'data_nm_labels': [],
            'data_nm_series': [],
        }

    if isinstance(resultado, dict):
        historico_entries = resultado.get('fcf_historico')
        proyectado_entries = resultado.get('fcf_proyectado')
        datos_emp = resultado.get('datos_empresa') or {}
    else:
        historico_entries = getattr(resultado, 'fcf_historico', None)
        proyectado_entries = getattr(resultado, 'fcf_proyectado', None)
        datos_emp = getattr(resultado, 'datos_empresa', None) or {}

    historico_labels, historico_series = _extract_chart_series(historico_entries)
    proyectado_labels, proyectado_series = _extract_chart_series(proyectado_entries)

    if isinstance(datos_emp, dict):
        rev_entries = datos_emp.get('revenue_historico_labeled')
        ni_entries = datos_emp.get('net_income_historico_labeled')
    else:
        rev_entries = getattr(datos_emp, 'revenue_historico_labeled', None)
        ni_entries = getattr(datos_emp, 'net_income_historico_labeled', None)

    rev_labels, rev_series = _extract_chart_series(rev_entries)
    ni_labels, ni_series = _extract_chart_series(ni_entries)

    if isinstance(datos_emp, dict):
        gm_entries = datos_emp.get('gross_margin_historico_labeled')
        nm_entries = datos_emp.get('net_margin_historico_labeled_pct')
    else:
        gm_entries = getattr(datos_emp, 'gross_margin_historico_labeled', None)
        nm_entries = getattr(datos_emp, 'net_margin_historico_labeled_pct', None)
    gm_labels, gm_series = _extract_chart_series(gm_entries)
    nm_labels, nm_series = _extract_chart_series(nm_entries)

    # TTM values to append as the most-current data point
    _get = (lambda k: datos_emp.get(k) if isinstance(datos_emp, dict) else getattr(datos_emp, k, None))
    fcf_ttm_b = _clean_numeric(_get('fcf_ttm_billones'))
    rev_ttm_b = _clean_numeric(_get('revenue_ttm_billones'))
    ni_ttm_b  = _clean_numeric(_get('net_income_ttm_billones'))
    gm_ttm    = _clean_numeric(_get('gross_margin_pct'))
    nm_ttm    = _clean_numeric(_get('net_margin_pct'))

    def _with_ttm(labels, series, ttm_val):
        lbls = list(reversed(labels))
        vals = list(reversed(series))
        if ttm_val is not None and lbls:
            lbls = lbls + ['TTM']
            vals = vals + [ttm_val]
        return lbls, vals

    data_fcf_labels, data_fcf_series = _with_ttm(historico_labels, historico_series, fcf_ttm_b)
    data_rev_labels, data_rev_series = _with_ttm(rev_labels, rev_series, rev_ttm_b)
    data_ni_labels,  data_ni_series  = _with_ttm(ni_labels,  ni_series,  ni_ttm_b)
    data_gm_labels,  data_gm_series  = _with_ttm(gm_labels,  gm_series,  gm_ttm)
    data_nm_labels,  data_nm_series  = _with_ttm(nm_labels,  nm_series,  nm_ttm)

    has_data = bool(historico_labels or proyectado_labels or rev_labels or ni_labels)
    return {
        'has_resultado': has_data,
        'fcf_historico_labels': historico_labels,
        'fcf_historico_series': historico_series,
        'fcf_proyectado_labels': proyectado_labels,
        'fcf_proyectado_series': proyectado_series,
        'revenue_historico_labels': list(reversed(rev_labels)),
        'revenue_historico_series': list(reversed(rev_series)),
        'net_income_historico_labels': list(reversed(ni_labels)),
        'net_income_historico_series': list(reversed(ni_series)),
        'data_fcf_labels': data_fcf_labels,
        'data_fcf_series': data_fcf_series,
        'data_rev_labels': data_rev_labels,
        'data_rev_series': data_rev_series,
        'data_ni_labels': data_ni_labels,
        'data_ni_series': data_ni_series,
        'data_gm_labels': data_gm_labels,
        'data_gm_series': data_gm_series,
        'data_nm_labels': data_nm_labels,
        'data_nm_series': data_nm_series,
    }


NEWS_PAGE_SIZE = 6
RECENT_HISTORY_VISIBLE_LIMIT = 5
RECENT_HISTORY_FETCH_LIMIT = 25


def _guardar_analisis(
    *,
    ticker: str,
    company_name: str,
    company_exchange: str,
    resultado: dict[str, Any] | None,
):
    if not ticker or not isinstance(resultado, dict):
        return None

    nombre_empresa = (company_name or resultado.get("nombre") or ticker).strip()
    sector = (resultado.get("sector") or "").strip()
    fuente_utilizada = (resultado.get("fuente_datos") or "").strip()
    metodo = (
        (resultado.get("datos_empresa") or {}).get("metodo_crecimiento_codigo")
        or AnalysisRecord.METODO_CAGR
    )

    valor_intrinseco = _to_decimal(resultado.get("valor_intrinseco"), places=4)
    precio_actual = _to_decimal(resultado.get("precio_actual"), places=4)
    diferencia_pct = _to_decimal(resultado.get("diferencia_pct"), places=2)

    ventana_reciente = timezone.now() - timedelta(minutes=5)
    duplicado = AnalysisRecord.objects.filter(
        ticker=ticker,
        metodo=metodo,
        fuente_utilizada=fuente_utilizada,
        valor_intrinseco=valor_intrinseco,
        precio_actual=precio_actual,
        diferencia_pct=diferencia_pct,
        created_at__gte=ventana_reciente,
    ).exists()

    if duplicado:
        return None

    return AnalysisRecord.objects.create(
        ticker=ticker,
        company_name=nombre_empresa,
        company_exchange=company_exchange,
        sector=sector,
        metodo=metodo,
        fuente_solicitada=_AUTO_FUENTE,
        fuente_utilizada=fuente_utilizada,
        valor_intrinseco=valor_intrinseco,
        precio_actual=precio_actual,
        diferencia_pct=diferencia_pct,
        estado=(resultado.get("estado") or "").strip(),
    )


def _serialize_news_item(item: dict) -> dict:
    fecha = item.get("fecha")
    fecha_iso = None
    fecha_display = None
    if isinstance(fecha, datetime):
        fecha_local = timezone.localtime(fecha) if timezone.is_aware(fecha) else fecha
        fecha_iso = fecha_local.isoformat()
        fecha_display = fecha_local.strftime("%d/%m/%Y %H:%M")

    return {
        "titulo": item.get("titulo"),
        "fuente": item.get("fuente"),
        "resumen": item.get("resumen"),
        "url": item.get("url"),
        "imagen": item.get("imagen"),
        "fecha_iso": fecha_iso,
        "fecha_display": fecha_display,
    }


def _parse_page(value: str | None, default: int = 1) -> int:
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    try:
        numero = int(value)
        return numero if numero > 0 else default
    except (TypeError, ValueError):
        return default


def landing(request):
    return render(request, 'landing.html')


_TICKER_STRIP_SYMBOLS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "BRK-B", "JPM", "V"]
_TICKER_STRIP_CACHE_TTL = 300  # 5 minutos


@require_GET
def ticker_strip_view(request):
    """Devuelve precios y variación diaria de los tickers del strip (JSON, caché 5 min)."""
    import yfinance as yf

    cache_key = "ticker_strip_data"
    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse({"tickers": cached})

    try:
        data = yf.download(
            _TICKER_STRIP_SYMBOLS,
            period="2d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        results = []
        close = data["Close"] if "Close" in data.columns else data.xs("Close", axis=1, level=0)
        for sym in _TICKER_STRIP_SYMBOLS:
            display = sym.replace("-", ".")
            try:
                series = close[sym].dropna()
                if len(series) < 2:
                    continue
                prev, last = float(series.iloc[-2]), float(series.iloc[-1])
                chg = (last - prev) / prev * 100
                results.append({
                    "sym": display,
                    "price": f"{last:.2f}",
                    "chg": f"{chg:+.2f}%",
                    "up": chg >= 0,
                })
            except Exception:
                continue

        cache.set(cache_key, results, _TICKER_STRIP_CACHE_TTL)
        return JsonResponse({"tickers": results})
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


def dcf_view(request):
    resultado = None
    error = None
    ticker = ""
    valor_busqueda = request.GET.get("company_query", "").strip()
    company_name = request.GET.get("company_name", "").strip()
    company_exchange = request.GET.get("company_exchange", "").strip()

    page_number = _parse_page(request.GET.get("page"))

    if request.method == "POST":
        valor_busqueda = request.POST.get("company_query", "").strip()
        company_name = request.POST.get("company_name", "").strip()
        company_exchange = request.POST.get("company_exchange", "").strip()
        ticker_resuelto = _resolver_ticker(request.POST.get("ticker", ""), valor_busqueda)

        if ticker_resuelto:
            query_params = {
                "ticker": ticker_resuelto,
                "company_query": valor_busqueda,
                "company_name": company_name,
                "company_exchange": company_exchange,
                "page": 1,
            }
            return redirect(f"{reverse('home')}?{urlencode(query_params)}")

        error = "Por favor ingresa un ticker válido."

    ticker = request.GET.get("ticker", "").strip().upper()

    if ticker:
        eligibility_error = _check_ticker_eligibility(ticker)
        if eligibility_error:
            error = eligibility_error
        else:
            try:
                resultado = _cached_ejecutar_dcf(ticker)
            except Exception as exc:
                error = f"Ocurrió un error al analizar el ticker: {exc}"
                resultado = None
            else:
                _guardar_analisis(
                    ticker=ticker,
                    company_name=company_name,
                    company_exchange=company_exchange,
                    resultado=resultado,
                )

    company_stage = None
    multi_model = None
    insider_data = {"disponible": False, "mensaje": "Sin ticker disponible para consultar insider trading."}
    analyst_data: dict = {"disponible": False, "mensaje": "Sin ticker disponible para consultar estimaciones de analistas."}
    filtros_etapa = []
    if resultado and isinstance(resultado, dict):
        try:
            company_stage = detect_company_stage(ticker, resultado)
        except Exception:
            company_stage = None

        try:
            stage_num = (company_stage or {}).get("stage", 4)
            wacc_val = (resultado.get("metricas") or {}).get("wacc") or 0.08
            multi_model = run_all_models(ticker, resultado, stage_num, wacc_val)
        except Exception:
            multi_model = None

        try:
            filtros_etapa = build_filtros_por_etapa(resultado, stage_num)
        except Exception:
            filtros_etapa = resultado.get("filtros") or []

        try:
            insider_data = get_insider_trading(ticker)
        except Exception:
            insider_data = {
                "disponible": False,
                "mensaje": "No se pudo consultar insider trading para este ticker.",
            }

        try:
            _precio_actual_raw = resultado.get("precio_actual") if isinstance(resultado, dict) else None
            _precio_actual_float = float(_precio_actual_raw) if _precio_actual_raw is not None else None
            analyst_data = get_analyst_estimates(ticker, precio_actual=_precio_actual_float)
        except Exception:
            analyst_data = {
                "disponible": False,
                "mensaje": "No se pudo consultar estimaciones de analistas.",
            }

        # Recalcular posiciones de la barra con rango dinámico que incluye todos los marcadores
        try:
            _dcf_precio = float((multi_model or {}).get("consenso", {}).get("precio") or 0) or None
            _po = dict((analyst_data.get("precio_objetivo") or {}))
            _bajo, _alto = _po.get("bajo"), _po.get("alto")
            _precio_actual_bar = analyst_data.get("precio_actual")
            _medio = _po.get("medio")

            if _bajo is not None and _alto is not None and float(_alto) > float(_bajo):
                # Rango visual: abarca TODOS los precios (analista + actuales) + 10% de padding
                # Los ticks de Mín/Máx se posicionan dentro del bar en su lugar real
                _candidatos = [float(p) for p in [_bajo, _alto, _precio_actual_bar, _medio] if p is not None]
                if _dcf_precio:
                    _candidatos.append(float(_dcf_precio))
                _vis_min = min(_candidatos)
                _vis_max = max(_candidatos)
                _vis_span_raw = (_vis_max - _vis_min) or 1.0
                _vis_low  = _vis_min - 0.10 * _vis_span_raw
                _vis_span = _vis_span_raw * 1.20

                def _a_pct(v):
                    if v is None:
                        return None
                    return round(max(0.0, min(100.0, (float(v) - _vis_low) / _vis_span * 100)), 1)

                # Posiciones de los marcadores de precio
                _po["precio_actual_pct"] = _a_pct(_precio_actual_bar)
                _po["medio_pct"]         = _a_pct(_medio)
                # Posiciones de los ticks del rango analista (Mín / Máx)
                _po["bajo_bar_pct"]  = _a_pct(float(_bajo))
                _po["alto_bar_pct"]  = _a_pct(float(_alto))
                if _dcf_precio:
                    _po["dcf_pct"]    = _a_pct(_dcf_precio)
                    _po["dcf_precio"] = round(_dcf_precio, 2)
                analyst_data = {**analyst_data, "precio_objetivo": _po}
        except Exception:
            pass

    chart_data = _build_chart_data(resultado)

    if isinstance(resultado, dict):
        raw_news = resultado.get("noticias") or []
    else:
        raw_news = getattr(resultado, "noticias", []) or []
    try:
        news_list: list[dict[str, Any]] = list(raw_news)
    except TypeError:
        news_list = []
    news_total = len(news_list)
    total_pages = max(1, (news_total + NEWS_PAGE_SIZE - 1) // NEWS_PAGE_SIZE) if news_total else 1
    if page_number > total_pages:
        page_number = total_pages
    if page_number < 1:
        page_number = 1

    base_query = request.GET.copy()
    if "page" in base_query:
        del base_query["page"]
    base_query_string = base_query.urlencode()

    news_payload = [_serialize_news_item(item) for item in news_list]
    tradingview_symbol = ticker
    if company_exchange and ticker:
        tradingview_symbol = f"{company_exchange.upper()}:{ticker}"

    in_watchlist = WatchlistItem.objects.filter(ticker=ticker).exists() if ticker else False
    watchlist_groups = _watchlist_groups_for_picker(ticker)
    precio_historico = (resultado or {}).get("precio_historico") if resultado else None
    datos_empresa_context = resultado.get("datos_empresa") if isinstance(resultado, dict) else {}
    if not isinstance(datos_empresa_context, dict):
        datos_empresa_context = {}
    metricas_context = resultado.get("metricas") if isinstance(resultado, dict) else {}
    if not isinstance(metricas_context, dict):
        metricas_context = {}

    context = {
        "in_watchlist": in_watchlist,
        "watchlist_groups": watchlist_groups,
        "precio_historico": precio_historico,
        "net_income_ttm_billones": datos_empresa_context.get("net_income_ttm_billones"),
        "payout_ratio_pct": datos_empresa_context.get("payout_ratio_pct"),
        "rf_fuente": metricas_context.get("rf_fuente"),
        "deuda_corriente_billones": datos_empresa_context.get("deuda_corriente_billones"),
        "total_current_assets_billones": datos_empresa_context.get("total_current_assets_billones"),
        "total_liabilities_billones": datos_empresa_context.get("total_liabilities_billones"),
        "multi_model": multi_model,
        "insider_trading": insider_data,
        "analyst_estimates": analyst_data,
        "resultado": resultado,
        "error": error,
        "ticker": ticker,
        "search_value": valor_busqueda or ticker,
        "company_name": company_name,
        "company_exchange": company_exchange,
        "tradingview_symbol": tradingview_symbol,
        "chart_data": chart_data,
        "news_data": news_payload,
        "news_total": news_total,
        "news_page_size": NEWS_PAGE_SIZE,
        "news_initial_page": page_number,
        "news_base_query": base_query_string,
        "news_config": {
            "page_size": NEWS_PAGE_SIZE,
            "initial_page": page_number,
            "base_query": base_query_string,
        },
        "filtros_etapa": filtros_etapa,
        "company_stage": company_stage,
        "stage_labels": ["Startup", "Hyper Growth", "Break Even", "Op. Leverage", "Cap. Return", "Decline"],
        "all_stages": [
            {"stage": k, "nombre": v["nombre"], "descripcion_breve": v["descripcion_breve"], "color": v["color"]}
            for k, v in STAGE_META.items()
        ],
    }

    return render(request, "dcf_app/index.html", context)


def _render_pdf(template_name: str, context: dict) -> bytes | None:
    html = render_to_string(template_name, context)
    output = io.BytesIO()
    pdf_result = cast(Any, pisa.CreatePDF(html, dest=output, encoding="UTF-8"))
    if getattr(pdf_result, "err", 0):
        return None
    return output.getvalue()


def dcf_executive_report_view(request, ticker: str):
    ticker = (ticker or "").strip().upper()

    if not ticker:
        return HttpResponse("Ticker inválido", status=400)

    try:
        resultado = _cached_ejecutar_dcf(ticker)
    except Exception as exc:
        return HttpResponse(f"No se pudo generar el reporte ejecutivo: {exc}", status=500)

    if not resultado:
        return HttpResponse("No hay datos suficientes para generar el reporte ejecutivo", status=404)

    try:
        company_stage = detect_company_stage(ticker, resultado)
    except Exception:
        company_stage = None

    try:
        stage_num = (company_stage or {}).get("stage", 4)
        wacc_val = (resultado.get("metricas") or {}).get("wacc")
        multi_model = run_all_models(ticker, resultado, stage_num, wacc_val)
    except Exception as exc:
        return HttpResponse(f"No se pudo calcular el score ejecutivo: {exc}", status=500)

    modelos = (multi_model or {}).get("modelos") or {}
    consenso = (multi_model or {}).get("consenso") or {}
    modelos_consenso = [
        {
            "nombre": modelos[key].get("nombre"),
            "valor": modelos[key].get("valor"),
            "peso_pct": modelos[key].get("peso_pct") or 0,
        }
        for key in consenso.get("modelos_usados_keys", [])
        if key in modelos
    ]
    modelos_consenso.sort(key=lambda m: m["peso_pct"], reverse=True)
    modelos_consenso = modelos_consenso[:8]

    score_final = (multi_model or {}).get("score_final") or {}
    componentes_score = score_final.get("componentes") or {}
    etiquetas_componentes = {
        "upside": "Upside del consenso",
        "confianza": "Confianza / DR",
        "solvencia": "Solvencia / Altman",
        "fundamentals": "Filtros fundamentales",
    }
    componentes = []
    for key in ("upside", "confianza", "solvencia", "fundamentals"):
        item = componentes_score.get(key) or {}
        componentes.append({
            "nombre": etiquetas_componentes[key],
            "puntos": item.get("puntos"),
            "peso": item.get("peso"),
            "detalle": item.get("detalle"),
        })

    altman_z = modelos.get("altman_z") or {}
    az_zona_code = altman_z.get("zona_code") or ""
    az_zona_textos = {
        "safe": "Alta probabilidad de solvencia a largo plazo.",
        "grey": "Zona de incertidumbre — monitorear indicadores de deuda.",
        "distress": "Alta probabilidad de insolvencia en los próximos años.",
    }
    az_zona_text = az_zona_textos.get(az_zona_code, "Sin datos disponibles.")

    context = {
        "resultado": resultado,
        "ticker": ticker,
        "generado": timezone.now(),
        "multi_model": multi_model,
        "company_stage": company_stage,
        "score_final": score_final,
        "componentes_score": componentes,
        "modelos_consenso": modelos_consenso,
        "altman_z": altman_z,
        "az_zona_text": az_zona_text,
    }

    try:
        html_string = render_to_string("dcf_app/executive_report.html", context)
        from weasyprint import HTML as WeasyHTML  # lazy: evita fallo de startup en macOS/Railway
        pdf_bytes = WeasyHTML(string=html_string).write_pdf()
    except (ImportError, OSError):
        pdf_bytes = _render_pdf("dcf_app/executive_report.html", context)
        if pdf_bytes is None:
            return HttpResponse("No se pudo generar el reporte ejecutivo en PDF", status=500)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="Reporte_{ticker}.pdf"'
    return response


_BUSINESS_CYCLE_CACHE_TTL = 600  # 10 minutos


@require_GET
def business_cycle_view(request):
    """Devuelve la fase del ciclo económico detectada (JSON)."""
    cache_key = "business_cycle_phase"
    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    try:
        data = get_business_cycle_phase()
        cache.set(cache_key, data, _BUSINESS_CYCLE_CACHE_TTL)
        return JsonResponse(data)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@require_GET
def search_companies_view(request):
    """Devuelve coincidencias de tickers basadas en el texto ingresado."""

    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"results": []})

    try:
        limit = int(request.GET.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10

    limit = max(1, min(limit, 20))

    coincidencias = search_companies(query, limit=limit)

    def _serializar(item: CompanySearchResult) -> dict:
        return {
            "symbol": item.symbol,
            "name": item.name,
            "exchange": item.exchange,
            "type": item.asset_type,
        }

    return JsonResponse({"results": [_serializar(item) for item in coincidencias]})


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def watchlist_view(request):
    """Página principal de la watchlist con grupos."""
    groups = WatchlistGroup.objects.prefetch_related("items").all()
    return render(request, "dcf_app/watchlist.html", {"groups": groups})


@require_POST
def watchlist_toggle(request):
    """Agrega o quita un ticker de un grupo de la watchlist (JSON).

    Si se pasa group_id usa ese grupo; si no, usa el primer grupo existente
    o crea uno 'General' automáticamente.
    """
    ticker = (request.POST.get("ticker") or "").strip().upper()
    company_name = (request.POST.get("company_name") or "").strip()
    company_exchange = (request.POST.get("company_exchange") or "").strip()
    group_id = request.POST.get("group_id") or None

    if not ticker:
        return JsonResponse({"error": "Ticker requerido"}, status=400)

    if group_id:
        group = WatchlistGroup.objects.filter(id=group_id).first()
        if not group:
            return JsonResponse({"error": "Grupo no encontrado"}, status=404)
    else:
        group = WatchlistGroup.objects.filter(name__iexact="General").order_by("created_at").first()
        if not group:
            group = WatchlistGroup.objects.create(name="General")

    item = WatchlistItem.objects.filter(watchlist=group, ticker=ticker).first()
    if item:
        item.delete()
        in_watchlist = WatchlistItem.objects.filter(ticker=ticker).exists()
        return JsonResponse({"action": "removed", "ticker": ticker, "in_watchlist": in_watchlist})
    else:
        WatchlistItem.objects.create(
            watchlist=group,
            ticker=ticker,
            company_name=company_name,
            company_exchange=company_exchange,
        )
        return JsonResponse({"action": "added", "ticker": ticker, "group_id": group.id, "in_watchlist": True})


def _watchlist_groups_for_picker(ticker: str) -> list[dict[str, Any]]:
    """Devuelve las watchlists disponibles para el selector del análisis."""

    symbol = (ticker or "").strip().upper()
    groups = list(WatchlistGroup.objects.prefetch_related("items").all())
    picker_groups: list[dict[str, Any]] = []
    general_group = next((group for group in groups if group.name.strip().lower() == "general"), None)

    def contains(group: WatchlistGroup | None) -> bool:
        if not symbol or not group:
            return False
        return any((item.ticker or "").strip().upper() == symbol for item in group.items.all())

    picker_groups.append(
        {
            "id": general_group.id if general_group else "",
            "name": "General",
            "contains_ticker": contains(general_group),
            "is_default": True,
        }
    )

    for group in groups:
        if general_group and group.id == general_group.id:
            continue
        picker_groups.append(
            {
                "id": group.id,
                "name": group.name,
                "contains_ticker": contains(group),
                "is_default": False,
            }
        )

    return picker_groups


@require_GET
def watchlist_status(request):
    """Devuelve si un ticker está en cualquier grupo de la watchlist."""
    ticker = (request.GET.get("ticker") or "").strip().upper()
    if not ticker:
        return JsonResponse({"in_watchlist": False})
    in_watchlist = WatchlistItem.objects.filter(ticker=ticker).exists()
    return JsonResponse({"in_watchlist": in_watchlist, "ticker": ticker})


@require_POST
def watchlist_group_create(request):
    """Crea un nuevo grupo de watchlist."""
    name = (request.POST.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Nombre requerido"}, status=400)
    if len(name) > 100:
        name = name[:100]
    group = WatchlistGroup.objects.create(name=name)
    return JsonResponse({"id": group.id, "name": group.name})


@require_POST
def watchlist_group_delete(request):
    """Elimina un grupo y todos sus items."""
    group_id = request.POST.get("group_id") or None
    if not group_id:
        return JsonResponse({"error": "group_id requerido"}, status=400)
    deleted, _ = WatchlistGroup.objects.filter(id=group_id).delete()
    if not deleted:
        return JsonResponse({"error": "Grupo no encontrado"}, status=404)
    return JsonResponse({"action": "deleted", "group_id": group_id})


@require_POST
def watchlist_group_rename(request):
    """Renombra un grupo de watchlist."""
    group_id = request.POST.get("group_id") or None
    name = (request.POST.get("name") or "").strip()
    if not group_id or not name:
        return JsonResponse({"error": "group_id y name requeridos"}, status=400)
    updated = WatchlistGroup.objects.filter(id=group_id).update(name=name[:100])
    if not updated:
        return JsonResponse({"error": "Grupo no encontrado"}, status=404)
    return JsonResponse({"action": "renamed", "group_id": group_id, "name": name[:100]})


@require_GET
def watchlist_prices_view(request):
    """Devuelve precios actuales para una lista de tickers (uso de watchlist)."""
    tickers_param = (request.GET.get("tickers") or "").strip()
    if not tickers_param:
        return JsonResponse({"prices": {}})

    symbols = [s.strip().upper() for s in tickers_param.split(",") if s.strip()][:25]

    def _fetch(symbol: str) -> tuple[str, float | None]:
        try:
            import yfinance as yf
            fi = yf.Ticker(symbol).fast_info
            price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            return symbol, round(float(price), 2) if price and float(price) > 0 else None
        except Exception:
            return symbol, None

    with ThreadPoolExecutor(max_workers=min(len(symbols), 6)) as ex:
        prices = dict(ex.map(_fetch, symbols))

    return JsonResponse({"prices": prices})


def history_view(request):
    records = list(AnalysisRecord.objects.all())
    return render(request, "dcf_app/history.html", {"records": records})
