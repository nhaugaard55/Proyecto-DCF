from django.shortcuts import render
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
