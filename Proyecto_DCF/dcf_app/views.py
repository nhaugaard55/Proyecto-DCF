import io
import re
from decimal import Decimal
from typing import Any, cast
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET
from xhtml2pdf import pisa

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




def dcf_view(request):
    resultado = None
    error = None
    ticker = ""
    metodo = "1"
    fuente = "auto"
    valor_busqueda = ""
    company_name = ""
    company_exchange = ""

    if request.method == "POST":
        valor_busqueda = request.POST.get("company_query", "").strip()
        company_name = request.POST.get("company_name", "").strip()
        company_exchange = request.POST.get("company_exchange", "").strip()
        ticker = _resolver_ticker(request.POST.get("ticker", ""), valor_busqueda)
        metodo = request.POST.get("metodo", "1")
        fuente = request.POST.get("fuente", "auto")

        if ticker:
            try:
                resultado = ejecutar_dcf(ticker, metodo, fuente)
            except Exception as e:
                error = f"Ocurrió un error al analizar el ticker: {e}"
        else:
            error = "Por favor ingresá un ticker válido."

    chart_data = _build_chart_data(resultado)

    return render(request, "dcf_app/index.html", {
        "resultado": resultado,
        "error": error,
        "ticker": ticker,
        "metodo": metodo,
        "fuente": fuente,
        "search_value": valor_busqueda or ticker,
        "company_name": company_name,
        "company_exchange": company_exchange,
        "chart_data": chart_data,
    })


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
