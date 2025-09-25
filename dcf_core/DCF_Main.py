import yfinance as yf
from .finanzas import calcular_crecimientos
from .empresa import analizar_empresa


def ejecutar_dcf(ticker: str, metodo: str = "1") -> dict:
    """
    Ejecuta el análisis DCF para un ticker dado y devuelve un diccionario con los resultados clave.

    Args:
        ticker (str): Símbolo bursátil de la empresa.
        metodo (str): "1" para usar CAGR, "2" para promedio año a año. Default: "1".

    Returns:
        dict: Contiene 'precio_actual', 'valor_intrinseco', 'estado', 'diferencia_pct'.
    """
    empresa_temp = yf.Ticker(ticker)
    cashflow_temp = getattr(empresa_temp, "cashflow", None)

    fcf_temp = None
    if cashflow_temp is not None and not cashflow_temp.empty and "Free Cash Flow" in cashflow_temp.index:
        fcf_temp = cashflow_temp.loc["Free Cash Flow"].dropna().head(5)

    crecimiento, avg_growth_rate = calcular_crecimientos(fcf_temp)

    return analizar_empresa(ticker, metodo, crecimiento, avg_growth_rate)
