import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from xhtml2pdf import pisa

from accounts.subscription import can_run_analysis, get_usage_summary, record_analysis_run

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
    # Rechazar Infinity y NaN: SQLite los guarda como texto pero luego
    # decimal.quantize() lanza InvalidOperation al leerlos de vuelta.
    if dec_value.is_nan() or dec_value.is_infinite():
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
COUNTED_ANALYSES_SESSION_KEY = "counted_analysis_tickers"


def _get_counted_analysis_tickers(request) -> set[str]:
    """Return tickers already charged today for this browser session."""
    today_key = timezone.localdate().isoformat()
    payload = request.session.get(COUNTED_ANALYSES_SESSION_KEY) or {}
    if payload.get("date") != today_key:
        return set()
    tickers = payload.get("tickers") or []
    return {str(ticker).strip().upper() for ticker in tickers if ticker}


def _analysis_usage_already_counted(request, ticker: str) -> bool:
    symbol = (ticker or "").strip().upper()
    return bool(symbol and symbol in _get_counted_analysis_tickers(request))


def _mark_analysis_usage_counted(request, ticker: str) -> None:
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return

    tickers = _get_counted_analysis_tickers(request)
    tickers.add(symbol)
    request.session[COUNTED_ANALYSES_SESSION_KEY] = {
        "date": timezone.localdate().isoformat(),
        "tickers": sorted(tickers),
    }
    request.session.modified = True


def _guardar_analisis(
    *,
    user=None,
    ticker: str,
    company_name: str,
    company_exchange: str,
    resultado: dict[str, Any] | None,
    precio_consenso: float | None = None,
    veredicto_consenso: str | None = None,
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

    precio_actual_raw = resultado.get("precio_actual")
    precio_actual = _to_decimal(precio_actual_raw, places=4)

    # Preferir el precio consenso multi-modelo sobre el valor intrínseco DCF.
    # El consenso es el dato principal que el usuario ve en el análisis.
    if precio_consenso is not None:
        valor_intrinseco = _to_decimal(precio_consenso, places=4)
        # Recalcular diferencia_pct y estado con el consenso
        try:
            pa = float(precio_actual_raw or 0)
            if pa:
                diferencia_pct = _to_decimal((precio_consenso - pa) / pa * 100, places=2)
            else:
                diferencia_pct = None
        except Exception:
            diferencia_pct = None
        _VEREDICTO_MAP = {
            "Subvaluada": "SUBVALUADA",
            "Sobrevaluada": "SOBREVALUADA",
            "Precio Razonable": "RAZONABLE",
        }
        estado = _VEREDICTO_MAP.get(veredicto_consenso or "", "") or (resultado.get("estado") or "").strip()
    else:
        valor_intrinseco = _to_decimal(resultado.get("valor_intrinseco"), places=4)
        diferencia_pct = _to_decimal(resultado.get("diferencia_pct"), places=2)
        estado = (resultado.get("estado") or "").strip()

    ventana_reciente = timezone.now() - timedelta(minutes=5)
    duplicado = AnalysisRecord.objects.filter(
        user=user,
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
        user=user,
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
        estado=estado,
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
    analysis_limit_exceeded = False
    analysis_limit_summary = None
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
        should_charge_analysis = not _analysis_usage_already_counted(request, ticker)
        eligibility_error = _check_ticker_eligibility(ticker)
        if eligibility_error:
            error = eligibility_error
        elif should_charge_analysis and not can_run_analysis(request):
            analysis_limit_exceeded = True
            analysis_limit_summary = get_usage_summary(request)
            if request.user.is_authenticated:
                error = "Alcanzaste tu límite diario."
            else:
                error = "Llegaste al límite gratuito diario."
        else:
            try:
                resultado = _cached_ejecutar_dcf(ticker)
            except Exception as exc:
                error = f"Ocurrió un error al analizar el ticker: {exc}"
                resultado = None
            else:
                if should_charge_analysis:
                    record_analysis_run(request)
                    _mark_analysis_usage_counted(request, ticker)

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
            _precio_actual_raw = resultado.get("precio_actual") if isinstance(resultado, dict) else None
            _precio_actual_float = float(_precio_actual_raw) if _precio_actual_raw is not None else None
            analyst_data = get_analyst_estimates(ticker, precio_actual=_precio_actual_float)
        except Exception:
            analyst_data = {
                "disponible": False,
                "mensaje": "No se pudo consultar estimaciones de analistas.",
            }

        try:
            stage_num = (company_stage or {}).get("stage", 4)
            wacc_val = (resultado.get("metricas") or {}).get("wacc") or 0.08
            multi_model = run_all_models(ticker, resultado, stage_num, wacc_val, analyst_estimates=analyst_data)
        except Exception:
            multi_model = None

        # Guardar en historial con el precio consenso multi-modelo como valor principal.
        # Se ejecuta aquí, después de calcular multi_model, para tener el consenso real.
        try:
            _consenso = (multi_model or {}).get("consenso") or {}
            _guardar_analisis(
                user=request.user if request.user.is_authenticated else None,
                ticker=ticker,
                company_name=company_name,
                company_exchange=company_exchange,
                resultado=resultado,
                precio_consenso=_consenso.get("precio"),
                veredicto_consenso=_consenso.get("veredicto"),
            )
        except Exception:
            pass

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

    in_watchlist = (
        WatchlistItem.objects.filter(watchlist__user=request.user, ticker=ticker).exists()
        if ticker and request.user.is_authenticated
        else False
    )
    watchlist_groups = _watchlist_groups_for_picker(request, ticker)
    precio_historico = (resultado or {}).get("precio_historico") if resultado else None
    datos_empresa_context = resultado.get("datos_empresa") if isinstance(resultado, dict) else {}
    if not isinstance(datos_empresa_context, dict):
        datos_empresa_context = {}
    metricas_context = resultado.get("metricas") if isinstance(resultado, dict) else {}
    if not isinstance(metricas_context, dict):
        metricas_context = {}

    context = {
        "analysis_limit_exceeded": analysis_limit_exceeded,
        "analysis_limit_summary": analysis_limit_summary or get_usage_summary(request),
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
        "mostrar_debug": request.user.is_staff,
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


def _pdf_float(value: Any) -> float | None:
    if value in (None, "", "N/D"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pdf_money(value: Any, decimals: int = 0) -> str:
    number = _pdf_float(value)
    if number is None:
        return "N/D"
    sign = "-" if number < 0 else ""
    number_abs = abs(number)
    if number_abs >= 1_000_000_000_000:
        return f"{sign}${number_abs / 1_000_000_000_000:.2f}T"
    if number_abs >= 1_000_000_000:
        return f"{sign}${number_abs / 1_000_000_000:.2f}B"
    if number_abs >= 1_000_000:
        return f"{sign}${number_abs / 1_000_000:.2f}M"
    if number_abs >= 1_000:
        return f"{sign}${number_abs / 1_000:.0f}k"
    return f"{sign}${number_abs:.{decimals}f}"


def _pdf_price(value: Any) -> str:
    number = _pdf_float(value)
    if number is None:
        return "N/D"
    return f"${number:,.2f}"


def _pdf_pct(value: Any, decimals: int = 1, signed: bool = False) -> str:
    number = _pdf_float(value)
    if number is None:
        return "N/D"
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number:.{decimals}f}%"


def _pdf_ratio(value: Any, decimals: int = 2) -> str:
    number = _pdf_float(value)
    if number is None:
        return "N/D"
    return f"{number:.{decimals}f}x"


def _pdf_text(value: Any, fallback: str = "N/D") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _build_pdf_stage_segments(company_stage: dict | None) -> list[dict[str, Any]]:
    labels = ["Startup", "Hyper", "Break Even", "Op. Lev.", "Capital", "Decline"]
    active = int((company_stage or {}).get("stage") or 0)
    return [{"num": idx, "label": label, "active": idx == active} for idx, label in enumerate(labels, start=1)]


def _pdf_score_label(score: Any) -> str:
    value = _pdf_float(score)
    if value is None:
        return "Sin score disponible"
    if value < 3.5:
        return "Evitar / riesgo alto"
    if value < 6.5:
        return "Mantener / neutral"
    if value < 8:
        return "Atractiva"
    return "Alta convicción"


def _pdf_stage_description(company_stage: dict | None) -> str:
    stage = int((company_stage or {}).get("stage") or 0)
    descriptions = {
        1: "Empresa en fase inicial: prioridad en validación del modelo de negocio y acceso a capital.",
        2: "Empresa en hipercrecimiento: el foco está en expansión de ingresos, aun con presión sobre márgenes.",
        3: "Empresa cercana al punto de equilibrio: la transición a rentabilidad sostenible es el factor clave.",
        4: "Empresa en fase de escala operativa: el crecimiento debería traducirse en mayor eficiencia y márgenes.",
        5: "Empresa madura con retorno de capital: la calidad del flujo de caja y la disciplina de capital son centrales.",
        6: "Empresa en declive o reestructuración: conviene monitorear caída de ingresos, márgenes y solvencia.",
    }
    return descriptions.get(stage, "Etapa no disponible: interpretar el análisis con cautela por falta de señales suficientes.")


def _pdf_valid_model_range(modelos: dict[str, dict]) -> dict[str, Any]:
    values: list[float] = []
    for model in modelos.values():
        if not model.get("aplicable"):
            continue
        value = _pdf_float(model.get("valor"))
        if value is not None and value > 0:
            values.append(value)
    if not values:
        return {
            "minimum": None,
            "maximum": None,
            "count": 0,
            "range_display": "N/D",
        }
    ordered = sorted(values)
    return {
        "minimum": ordered[0],
        "maximum": ordered[-1],
        "count": len(ordered),
        "range_display": f"{_pdf_price(ordered[0])} - {_pdf_price(ordered[-1])}",
    }


def _pdf_confidence_level(consenso: dict[str, Any], model_count: int, datos_empresa: dict[str, Any], precio_actual: Any) -> str:
    dr = _pdf_float(consenso.get("disagreement_ratio"))
    dr_pct = dr * 100 if dr is not None and dr <= 3 else dr
    has_core_data = _pdf_float(precio_actual) is not None and (
        _pdf_float(datos_empresa.get("revenue_ttm")) is not None
        or _pdf_float(datos_empresa.get("fcf_ttm")) is not None
    )
    if model_count < 3 or (dr_pct is not None and dr_pct > 100) or not has_core_data:
        return "Baja"
    if model_count < 5 or (dr_pct is not None and dr_pct > 60):
        return "Media"
    return "Alta"


def _pdf_dispersion_warning(consenso: dict[str, Any]) -> dict[str, str] | None:
    dr = _pdf_float(consenso.get("disagreement_ratio"))
    if dr is None:
        return None
    dr_pct = dr * 100 if dr <= 3 else dr
    if dr_pct > 100:
        return {
            "level": "strong",
            "title": "Dispersión extrema",
            "text": "Dispersión extrema. El consenso debe interpretarse con alta cautela.",
        }
    if dr_pct > 60:
        return {
            "level": "warning",
            "title": "Alta dispersión entre modelos",
            "text": (
                "Alta dispersión entre modelos. El consenso puede estar afectado por outliers "
                "o diferencias importantes entre metodologías. Revisar la tabla de modelos antes "
                "de usar este valor como referencia."
            ),
        }
    return None


def _build_pdf_investment_thesis(
    consenso: dict[str, Any],
    datos_empresa: dict[str, Any],
    metricas: dict[str, Any],
    altman_z: dict[str, Any],
    insider_data: dict[str, Any],
) -> list[str]:
    bullets: list[str] = []
    upside = _pdf_float(consenso.get("upside_pct"))
    roe = _pdf_float(datos_empresa.get("roe_pct"))
    margin = _pdf_float(datos_empresa.get("net_margin_pct"))
    cagr_fcf = _pdf_float(metricas.get("crecimiento_pct"))
    dr = _pdf_float(consenso.get("disagreement_ratio"))
    dr_pct = (dr * 100 if dr is not None and dr <= 3 else dr)

    if upside is not None:
        if upside >= 20:
            bullets.append("El consenso de modelos sugiere potencial de revalorización frente al precio actual.")
        elif upside < 0:
            bullets.append("El consenso de modelos sugiere que el precio actual ya incorpora expectativas exigentes.")
        else:
            bullets.append("El consenso de modelos muestra un potencial moderado frente al precio actual.")
    if roe is not None and roe >= 15:
        bullets.append("La compañía muestra alta rentabilidad sobre capital.")
    if margin is not None and margin >= 15:
        bullets.append("Los márgenes muestran buena calidad relativa del negocio.")
    if cagr_fcf is not None and cagr_fcf < 5:
        bullets.append("El crecimiento del FCF muestra señales de desaceleración o baja expansión.")
    if dr_pct is not None and dr_pct > 60:
        bullets.append("Existe alta dispersión entre modelos, por lo que el consenso debe interpretarse con cautela.")
    if (altman_z.get("zona_code") or "") == "distress":
        bullets.append("La solvencia requiere monitoreo por señales de fragilidad financiera.")
    if (insider_data.get("score_sentimiento") or "").lower() == "bajista":
        bullets.append("La actividad insider reciente no aporta una señal positiva clara.")

    if not bullets:
        bullets.append("El reporte combina valuación, calidad financiera, solvencia y señales de mercado para contextualizar el precio actual.")
    return bullets[:4]


def _build_pdf_sources(resultado: dict[str, Any], insider_data: dict[str, Any], analyst_data: dict[str, Any]) -> list[str]:
    sources: list[str] = []

    def add(value: Any) -> None:
        for part in str(value or "").replace(",", " ").split():
            normalized = part.strip()
            if not normalized:
                continue
            label_map = {
                "fmp": "FMP",
                "yfinance": "yfinance",
                "yf": "yfinance",
                "finnhub": "Finnhub",
                "marketaux": "MarketAux",
            }
            label = label_map.get(normalized.lower(), normalized)
            if label not in sources:
                sources.append(label)

    add(resultado.get("fuente_datos"))
    add(resultado.get("noticias_fuente"))
    add(insider_data.get("fuente"))
    add(analyst_data.get("fuente"))
    if not sources:
        return ["datos de terceros"]
    return sources


def _pdf_logo_uri() -> str | None:
    logo_path = finders.find("dcf_app/img/intrinsic-logo.png")
    if not logo_path:
        return None
    return Path(logo_path).as_uri()


def admin_export_md_view(request, ticker: str):
    """Exporta el análisis completo como archivo Markdown — solo admin/staff."""
    from django.contrib.auth.decorators import user_passes_test
    from dcf_core.exportar_md import build_admin_md

    if not request.user.is_authenticated or not request.user.is_staff:
        return HttpResponse("Acceso denegado", status=403)

    ticker = (ticker or "").strip().upper()
    if not ticker:
        return HttpResponse("Ticker inválido", status=400)

    try:
        resultado = _cached_ejecutar_dcf(ticker)
    except Exception as exc:
        return HttpResponse(f"Error al ejecutar análisis: {exc}", status=500)

    if not resultado or not isinstance(resultado, dict):
        return HttpResponse("No hay datos para este ticker.", status=404)

    try:
        company_stage = detect_company_stage(ticker, resultado)
    except Exception:
        company_stage = None

    try:
        _precio = float(resultado.get("precio_actual") or 0) or None
        analyst_data = get_analyst_estimates(ticker, precio_actual=_precio)
    except Exception:
        analyst_data = {"disponible": False}

    try:
        stage_num = (company_stage or {}).get("stage", 4)
        wacc_val = (resultado.get("metricas") or {}).get("wacc") or 0.08
        multi_model = run_all_models(ticker, resultado, stage_num, wacc_val, analyst_estimates=analyst_data)
    except Exception:
        multi_model = None

    try:
        insider_data = get_insider_trading(ticker)
    except Exception:
        insider_data = {"disponible": False}

    contenido = build_admin_md(
        ticker=ticker,
        resultado=resultado,
        multi_model=multi_model,
        company_stage=company_stage,
        analyst_data=analyst_data,
        insider_data=insider_data,
    )

    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"intrinsic_{ticker}_{ts}.md"
    response = HttpResponse(contenido, content_type="text/markdown; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required   # CR-02: cierra acceso anónimo — anónimo → /accounts/login/?next=...
def dcf_executive_report_view(request, ticker: str):
    # ACTIVAR AL LANZAR PAGOS: PDF exclusivo Pro/Plus
    # from accounts.subscription import get_user_plan, PLAN_PRO, PLAN_ADMIN
    # plan = get_user_plan(request)
    # if plan not in (PLAN_PRO, PLAN_ADMIN):
    #     return redirect('/?#pricing')

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
        _precio_actual_exec = _pdf_float(resultado.get("precio_actual"))
        analyst_data = get_analyst_estimates(ticker, precio_actual=_precio_actual_exec)
    except Exception:
        analyst_data = {"disponible": False}

    try:
        stage_num = (company_stage or {}).get("stage", 4)
        wacc_val = (resultado.get("metricas") or {}).get("wacc")
        multi_model = run_all_models(ticker, resultado, stage_num, wacc_val, analyst_estimates=analyst_data)
    except Exception as exc:
        return HttpResponse(f"No se pudo calcular el score ejecutivo: {exc}", status=500)

    modelos = (multi_model or {}).get("modelos") or {}
    consenso = (multi_model or {}).get("consenso") or {}
    precio_actual = _pdf_float(resultado.get("precio_actual"))
    modelos_consenso = [
        {
            "nombre": modelos[key].get("nombre"),
            "valor": modelos[key].get("valor"),
            "valor_display": _pdf_price(modelos[key].get("valor")),
            "peso_pct": modelos[key].get("peso_pct") or 0,
            "upside_pct": modelos[key].get("upside_pct"),
            "upside_display": _pdf_pct(modelos[key].get("upside_pct"), signed=True),
            "bar_width": min(100, abs(_pdf_float(modelos[key].get("upside_pct")) or 0)),
            "bar_class": "positive" if (_pdf_float(modelos[key].get("upside_pct")) or 0) >= 0 else "negative",
        }
        for key in consenso.get("modelos_usados_keys", [])
        if key in modelos
    ]
    modelos_consenso.sort(key=lambda m: m["peso_pct"], reverse=True)
    modelos_consenso_filtrados = [m for m in modelos_consenso if (m.get("peso_pct") or 0) >= 5][:6]
    modelos_consenso = modelos_consenso_filtrados or modelos_consenso[:6]

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
            "puntos_display": "N/D" if item.get("puntos") is None else f"{float(item.get('puntos')):.1f}/10",
            "peso": item.get("peso"),
            "detalle": item.get("detalle"),
        })

    altman_z = (multi_model or {}).get("altman") or modelos.get("altman_z") or {}
    az_zona_code = altman_z.get("zona_code") or ""
    az_zona_textos = {
        "safe": "Alta probabilidad de solvencia a largo plazo.",
        "grey": "Zona de incertidumbre — monitorear indicadores de deuda.",
        "distress": "Alta probabilidad de insolvencia en los próximos años.",
    }
    az_zona_text = az_zona_textos.get(az_zona_code, "Sin datos disponibles.")

    datos_empresa = resultado.get("datos_empresa") if isinstance(resultado, dict) else {}
    if not isinstance(datos_empresa, dict):
        datos_empresa = {}
    metricas = resultado.get("metricas") if isinstance(resultado, dict) else {}
    if not isinstance(metricas, dict):
        metricas = {}

    try:
        insider_data = get_insider_trading(ticker)
    except Exception:
        insider_data = {"disponible": False}

    precio_objetivo = analyst_data.get("precio_objetivo") if isinstance(analyst_data, dict) else {}
    if not isinstance(precio_objetivo, dict):
        precio_objetivo = {}

    consenso_precio = consenso.get("precio")
    consenso_upside = consenso.get("upside_pct")
    score_value = score_final.get("score")
    score_display = f"{float(score_value):.1f}/10" if score_value is not None else "N/D"
    insider_sentiment = _pdf_text(insider_data.get("score_sentimiento"))
    analyst_consensus = _pdf_text(analyst_data.get("recomendacion_consenso"))
    valid_model_range = _pdf_valid_model_range(modelos)
    investment_thesis = _build_pdf_investment_thesis(consenso, datos_empresa, metricas, altman_z, insider_data)
    dispersion_warning = _pdf_dispersion_warning(consenso)
    generated_at = timezone.now()
    insider_summary = insider_data.get("resumen") if isinstance(insider_data, dict) else {}
    if not isinstance(insider_summary, dict):
        insider_summary = {}
    insider_ratio = _pdf_float(insider_summary.get("ratio_compras"))
    insider_ratio_pct = insider_ratio * 100 if insider_ratio is not None and insider_ratio <= 1 else insider_ratio
    insider_message = _pdf_text(insider_data.get("mensaje"), "Sin actividad relevante reportada en la ventana analizada.")

    pdf_summary = {
        "logo_uri": _pdf_logo_uri(),
        "company_name": _pdf_text(resultado.get("nombre"), ticker),
        "sector": _pdf_text(resultado.get("sector") or datos_empresa.get("sector")),
        "precio_actual": _pdf_price(precio_actual),
        "consenso": _pdf_price(consenso_precio),
        "upside": _pdf_pct(consenso_upside, signed=True),
        "score": score_display,
        "score_label": _pdf_score_label(score_value),
        "confidence": _pdf_confidence_level(consenso, valid_model_range["count"], datos_empresa, precio_actual),
        "recomendacion": _pdf_text(score_final.get("recomendacion"), "Mantener"),
        "stage_segments": _build_pdf_stage_segments(company_stage),
        "stage_line": (
            f"Etapa {(company_stage or {}).get('stage', 'N/D')} · "
            f"{(company_stage or {}).get('stage_name', 'N/D')} · "
            f"Conf. {(company_stage or {}).get('confidence', 'N/D')}"
            if company_stage else "Etapa no disponible"
        ),
        "stage_description": _pdf_stage_description(company_stage),
        "revenue": datos_empresa.get("revenue_ttm_display") or _pdf_money(datos_empresa.get("revenue_ttm")),
        "net_margin": _pdf_pct(datos_empresa.get("net_margin_pct")),
        "fcf": datos_empresa.get("fcf_ttm_display") or _pdf_money(datos_empresa.get("fcf_ttm")),
        "cagr_fcf": _pdf_pct(metricas.get("crecimiento_pct")),
        "roe": _pdf_pct(datos_empresa.get("roe_pct")),
        "altman": "N/D" if altman_z.get("z_score") is None else f"{float(altman_z.get('z_score')):.1f}",
        "altman_zona": _pdf_text(altman_z.get("zona") or az_zona_text),
        "debt_cap": _pdf_pct(datos_empresa.get("debt_to_capital_pct")),
        "current_ratio": _pdf_ratio(datos_empresa.get("current_ratio_raw")),
        "insider_sentiment": insider_sentiment if insider_sentiment == "N/D" else insider_sentiment.capitalize(),
        "insider_purchases": _pdf_money(insider_summary.get("valor_compras_usd")),
        "insider_sales": _pdf_money(insider_summary.get("valor_ventas_usd")),
        "insider_buy_ratio": _pdf_pct(insider_ratio_pct, decimals=0),
        "insider_message": insider_message,
        "analyst_target": _pdf_price(precio_objetivo.get("medio")),
        "analyst_consensus": analyst_consensus if analyst_consensus == "N/D" else analyst_consensus.capitalize(),
        "analyst_count": _pdf_text(precio_objetivo.get("num_analistas")),
        "consensus_price": _pdf_price(consenso_precio),
        "consensus_upside": _pdf_pct(consenso_upside, signed=True),
        "model_range": valid_model_range["range_display"],
        "model_count": valid_model_range["count"],
        "data_price_timestamp": _pdf_text(resultado.get("precio_fecha") or resultado.get("precio_actual_fecha"), "Fuente: datos de terceros"),
        "data_financials_period": _pdf_text(datos_empresa.get("ultimo_periodo") or datos_empresa.get("periodo_financiero"), "Fuente: datos de terceros"),
        "generated_at": timezone.localtime(generated_at).strftime("%d/%m/%Y %H:%M"),
        "sources": ", ".join(_build_pdf_sources(resultado, insider_data, analyst_data)),
    }

    consensus_bar_width = min(100, abs(_pdf_float(consenso_upside) or 0))
    consensus_bar_class = "positive" if (_pdf_float(consenso_upside) or 0) >= 0 else "negative"

    context = {
        "resultado": resultado,
        "ticker": ticker,
        "generado": generated_at,
        "multi_model": multi_model,
        "company_stage": company_stage,
        "pdf_summary": pdf_summary,
        "investment_thesis": investment_thesis,
        "dispersion_warning": dispersion_warning,
        "score_final": score_final,
        "componentes_score": componentes,
        "modelos_consenso": modelos_consenso,
        "consensus_bar_width": consensus_bar_width,
        "consensus_bar_class": consensus_bar_class,
        "altman_z": altman_z,
        "az_zona_text": az_zona_text,
        "insider_trading": insider_data,
        "analyst_estimates": analyst_data,
        "mostrar_debug": request.user.is_staff,
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

def _requires_login_response() -> JsonResponse:
    return JsonResponse({
        "ok": False,
        "requires_login": True,
        "message": "Iniciá sesión para guardar tu watchlist.",
    })


def get_user_watchlist_groups(request):
    """Watchlist groups visible to the current authenticated user."""
    if not request.user.is_authenticated:
        return WatchlistGroup.objects.none()
    return WatchlistGroup.objects.filter(user=request.user)


def get_or_create_default_watchlist(request):
    """Return the user's General watchlist, creating it if necessary."""
    if not request.user.is_authenticated:
        return None
    group = (
        WatchlistGroup.objects
        .filter(user=request.user, name__iexact="General")
        .order_by("created_at")
        .first()
    )
    if group:
        return group
    return WatchlistGroup.objects.create(user=request.user, name="General")


def user_can_access_watchlist_group(request, group: WatchlistGroup | None) -> bool:
    return bool(
        group
        and request.user.is_authenticated
        and group.user_id == request.user.id
    )


def watchlist_view(request):
    """Página principal de la watchlist con grupos."""
    groups = get_user_watchlist_groups(request).prefetch_related("items")
    return render(request, "dcf_app/watchlist.html", {
        "groups": groups,
        "watchlist_requires_login": not request.user.is_authenticated,
    })


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

    if not request.user.is_authenticated:
        return _requires_login_response()

    if not ticker:
        return JsonResponse({"error": "Ticker requerido"}, status=400)

    if group_id:
        group = get_user_watchlist_groups(request).filter(id=group_id).first()
        if not user_can_access_watchlist_group(request, group):
            return JsonResponse({"error": "Grupo no encontrado"}, status=404)
    else:
        group = get_or_create_default_watchlist(request)

    item = WatchlistItem.objects.filter(watchlist=group, ticker=ticker).first()
    if item:
        item.delete()
        in_watchlist = WatchlistItem.objects.filter(watchlist__user=request.user, ticker=ticker).exists()
        return JsonResponse({"action": "removed", "ticker": ticker, "in_watchlist": in_watchlist})
    else:
        WatchlistItem.objects.create(
            watchlist=group,
            ticker=ticker,
            company_name=company_name,
            company_exchange=company_exchange,
        )
        return JsonResponse({"action": "added", "ticker": ticker, "group_id": group.id, "in_watchlist": True})


def _watchlist_groups_for_picker(request, ticker: str) -> list[dict[str, Any]]:
    """Devuelve las watchlists disponibles para el selector del análisis."""

    if not request.user.is_authenticated:
        return []

    symbol = (ticker or "").strip().upper()
    groups = list(get_user_watchlist_groups(request).prefetch_related("items"))
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
    if not ticker or not request.user.is_authenticated:
        return JsonResponse({"in_watchlist": False})
    in_watchlist = WatchlistItem.objects.filter(watchlist__user=request.user, ticker=ticker).exists()
    return JsonResponse({"in_watchlist": in_watchlist, "ticker": ticker})


@require_POST
def watchlist_group_create(request):
    """Crea un nuevo grupo de watchlist."""
    if not request.user.is_authenticated:
        return _requires_login_response()
    name = (request.POST.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Nombre requerido"}, status=400)
    if len(name) > 100:
        name = name[:100]
    group = WatchlistGroup.objects.create(user=request.user, name=name)
    return JsonResponse({"id": group.id, "name": group.name})


@require_POST
def watchlist_group_delete(request):
    """Elimina un grupo y todos sus items."""
    if not request.user.is_authenticated:
        return _requires_login_response()
    group_id = request.POST.get("group_id") or None
    if not group_id:
        return JsonResponse({"error": "group_id requerido"}, status=400)
    deleted, _ = get_user_watchlist_groups(request).filter(id=group_id).delete()
    if not deleted:
        return JsonResponse({"error": "Grupo no encontrado"}, status=404)
    return JsonResponse({"action": "deleted", "group_id": group_id})


@require_POST
def watchlist_group_rename(request):
    """Renombra un grupo de watchlist."""
    if not request.user.is_authenticated:
        return _requires_login_response()
    group_id = request.POST.get("group_id") or None
    name = (request.POST.get("name") or "").strip()
    if not group_id or not name:
        return JsonResponse({"error": "group_id y name requeridos"}, status=400)
    updated = get_user_watchlist_groups(request).filter(id=group_id).update(name=name[:100])
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
    if request.user.is_authenticated:
        records = _cargar_historial_seguro(request.user)
    else:
        records = []
    return render(request, "dcf_app/history.html", {"records": records})


@require_POST
def history_delete_record(request, record_id: int):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Autenticación requerida"}, status=401)
    deleted, _ = AnalysisRecord.objects.filter(pk=record_id, user=request.user).delete()
    if not deleted:
        return JsonResponse({"ok": False, "error": "Registro no encontrado"}, status=404)
    return JsonResponse({"ok": True})


@require_POST
def history_delete_all(request):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Autenticación requerida"}, status=401)
    AnalysisRecord.objects.filter(user=request.user).delete()
    return JsonResponse({"ok": True})


def _cargar_historial_seguro(user):
    """
    Carga el historial tolerando registros con DecimalField inválidos.

    Estrategia:
      1. Limpiar valores Infinity/NaN en la DB (raw SQL, no usa ORM decimal).
      2. Intentar la query normal.
      3. Si aún falla, obtener solo los IDs (enteros, sin conversión decimal)
         y cargar cada registro individualmente, saltando los problemáticos.
    """
    # Paso 1: sanear la DB antes de la query principal
    try:
        _limpiar_decimales_invalidos()
    except Exception:
        pass

    # Paso 2: query normal
    try:
        return list(AnalysisRecord.objects.filter(user=user))
    except Exception:
        pass

    # Paso 3: fallback — IDs primero (sin conversión decimal), luego uno a uno
    try:
        ids = list(
            AnalysisRecord.objects.filter(user=user)
            .order_by("-created_at")
            .values_list("id", flat=True)
        )
    except Exception:
        return []

    records = []
    for pk in ids:
        try:
            records.append(AnalysisRecord.objects.get(pk=pk))
        except Exception:
            pass
    return records


def _limpiar_decimales_invalidos():
    """
    Pone a NULL los campos DecimalField que contienen Infinity/NaN en
    la tabla de historial. Estos valores son válidos para Decimal de Python
    pero Django/SQLite no puede leerlos de vuelta con quantize().
    Usa SQL raw para evitar el conversor decimal del ORM.
    """
    from django.db import connection
    _CAMPOS = ("valor_intrinseco", "precio_actual", "diferencia_pct")
    # Cubre todas las variantes que SQLite puede haber almacenado
    _INVALIDOS = ("Infinity", "-Infinity", "NaN", "inf", "-inf", "nan",
                  "Inf", "-Inf")
    placeholders = ",".join(["?"] * len(_INVALIDOS))
    with connection.cursor() as cursor:
        for campo in _CAMPOS:
            cursor.execute(
                f"UPDATE dcf_app_analysisrecord SET {campo} = NULL "
                f"WHERE CAST({campo} AS TEXT) IN ({placeholders})",
                _INVALIDOS,
            )
