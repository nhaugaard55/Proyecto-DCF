import io
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from xhtml2pdf import pisa

from dcf_core.DCF_Main import ejecutar_dcf


def dcf_view(request):
    resultado = None
    error = None
    ticker = ""
    metodo = "1"

    if request.method == "POST":
        ticker = request.POST.get("ticker", "").strip().upper()
        metodo = request.POST.get("metodo", "1")

        if ticker:
            try:
                resultado = ejecutar_dcf(ticker, metodo)
            except Exception as e:
                error = f"Ocurrió un error al analizar el ticker: {e}"
        else:
            error = "Por favor ingresá un ticker válido."

    return render(request, "dcf_app/index.html", {
        "resultado": resultado,
        "error": error,
        "ticker": ticker,
        "metodo": metodo
    })


def _render_pdf(template_name: str, context: dict) -> bytes | None:
    html = render_to_string(template_name, context)
    output = io.BytesIO()
    pdf = pisa.CreatePDF(html, dest=output, encoding="UTF-8")
    if pdf.err:
        return None
    return output.getvalue()


def dcf_pdf_view(request):
    ticker = request.GET.get("ticker", "").strip().upper()
    metodo = request.GET.get("metodo", "1")

    if not ticker:
        return HttpResponse("Ticker inválido", status=400)

    try:
        resultado = ejecutar_dcf(ticker, metodo)
    except Exception as exc:
        return HttpResponse(f"No se pudo generar el informe: {exc}", status=500)

    if not resultado:
        return HttpResponse("No hay datos suficientes para generar el informe", status=404)

    context = {
        "resultado": resultado,
        "ticker": ticker,
        "metodo": "CAGR" if metodo != "2" else "Promedio",
        "generado": timezone.now(),
    }

    pdf_bytes = _render_pdf("dcf_app/pdf_report.html", context)
    if pdf_bytes is None:
        return HttpResponse("Error generando PDF", status=500)

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="DCF_{ticker}.pdf"'
    return response
