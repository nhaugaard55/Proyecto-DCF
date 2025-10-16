import io
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, cast
from urllib.parse import urlencode

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from xhtml2pdf import pisa

from .models import AnalysisRecord

from dcf_core.DCF_Main import ejecutar_dcf
from dcf_core.search import CompanySearchResult, search_companies


_SYMBOL_PATTERN = re.compile(r"^\s*([A-Za-z0-9.\-:]+)")


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


def _normalize_metodo(value: str | None) -> str:
    return value if value in ("1", "2") else "1"


def _normalize_fuente(value: str | None) -> str:
    permitido = {"auto", "fmp", "yfinance"}
    value = (value or "auto").lower()
    return value if value in permitido else "auto"


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
        }

    if isinstance(resultado, dict):
        historico_entries = resultado.get('fcf_historico')
        proyectado_entries = resultado.get('fcf_proyectado')
    else:
        historico_entries = getattr(resultado, 'fcf_historico', None)
        proyectado_entries = getattr(resultado, 'fcf_proyectado', None)

    historico_labels, historico_series = _extract_chart_series(historico_entries)
    proyectado_labels, proyectado_series = _extract_chart_series(proyectado_entries)

    has_data = bool(historico_labels or proyectado_labels)
    return {
        'has_resultado': has_data,
        'fcf_historico_labels': historico_labels,
        'fcf_historico_series': historico_series,
        'fcf_proyectado_labels': proyectado_labels,
        'fcf_proyectado_series': proyectado_series,
    }


NEWS_PAGE_SIZE = 6
RECENT_HISTORY_VISIBLE_LIMIT = 5
RECENT_HISTORY_FETCH_LIMIT = 25


def _guardar_analisis(
    *,
    ticker: str,
    metodo: str,
    fuente_solicitada: str,
    company_name: str,
    company_exchange: str,
    resultado: dict[str, Any] | None,
):
    if not ticker or not isinstance(resultado, dict):
        return None

    nombre_empresa = (company_name or resultado.get("nombre") or ticker).strip()
    sector = (resultado.get("sector") or "").strip()
    fuente_utilizada = (resultado.get("fuente_datos") or "").strip()

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
        fuente_solicitada=fuente_solicitada,
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


def dcf_view(request):
    resultado = None
    error = None
    ticker = ""
    metodo = _normalize_metodo(request.GET.get("metodo"))
    fuente = _normalize_fuente(request.GET.get("fuente"))
    valor_busqueda = request.GET.get("company_query", "").strip()
    company_name = request.GET.get("company_name", "").strip()
    company_exchange = request.GET.get("company_exchange", "").strip()

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

    page_number = _parse_page(request.GET.get("page"))

    if request.method == "POST":
        valor_busqueda = request.POST.get("company_query", "").strip()
        company_name = request.POST.get("company_name", "").strip()
        company_exchange = request.POST.get("company_exchange", "").strip()
        metodo = _normalize_metodo(request.POST.get("metodo"))
        fuente = _normalize_fuente(request.POST.get("fuente"))
        ticker_resuelto = _resolver_ticker(request.POST.get("ticker", ""), valor_busqueda)

        if ticker_resuelto:
            query_params = {
                "ticker": ticker_resuelto,
                "metodo": metodo,
                "fuente": fuente,
                "company_query": valor_busqueda,
                "company_name": company_name,
                "company_exchange": company_exchange,
                "page": 1,
            }
            return redirect(f"{reverse('home')}?{urlencode(query_params)}")

        error = "Por favor ingresá un ticker válido."

    ticker = request.GET.get("ticker", "").strip().upper()

    if ticker:
        try:
            resultado = ejecutar_dcf(ticker, metodo, fuente)
        except Exception as exc:
            error = f"Ocurrió un error al analizar el ticker: {exc}"
            resultado = None
        else:
            _guardar_analisis(
                ticker=ticker,
                metodo=metodo,
                fuente_solicitada=fuente,
                company_name=company_name,
                company_exchange=company_exchange,
                resultado=resultado,
            )

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
    recent_records_queryset = AnalysisRecord.objects.all()
    recent_records = list(recent_records_queryset[:RECENT_HISTORY_FETCH_LIMIT])

    context = {
        "resultado": resultado,
        "error": error,
        "ticker": ticker,
        "metodo": metodo,
        "fuente": fuente,
        "search_value": valor_busqueda or ticker,
        "company_name": company_name,
        "company_exchange": company_exchange,
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
        "recent_records": recent_records,
        "recent_records_limit": RECENT_HISTORY_VISIBLE_LIMIT,
    }

    return render(request, "dcf_app/index.html", context)


def _render_pdf(template_name: str, context: dict) -> bytes | None:
    html = render_to_string(template_name, context)
    output = io.BytesIO()
    pdf_result = cast(Any, pisa.CreatePDF(html, dest=output, encoding="UTF-8"))
    if getattr(pdf_result, "err", 0):
        return None
    return output.getvalue()


def dcf_pdf_view(request):
    ticker = request.GET.get("ticker", "").strip().upper()
    metodo = request.GET.get("metodo", "1")
    fuente = request.GET.get("fuente", "auto")

    if not ticker:
        return HttpResponse("Ticker inválido", status=400)

    try:
        resultado = ejecutar_dcf(ticker, metodo, fuente)
    except Exception as exc:
        return HttpResponse(f"No se pudo generar el informe: {exc}", status=500)

    if not resultado:
        return HttpResponse("No hay datos suficientes para generar el informe", status=404)

    context = {
        "resultado": resultado,
        "ticker": ticker,
        "metodo": "CAGR" if metodo != "2" else "Promedio",
        "fuente": fuente,
        "generado": timezone.now(),
    }

    pdf_bytes = _render_pdf("dcf_app/pdf_report.html", context)
    if pdf_bytes is None:
        return HttpResponse("Error generando PDF", status=500)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="DCF_{ticker}.pdf"'
    return response


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
