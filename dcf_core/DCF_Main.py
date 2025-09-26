from typing import List

import yfinance as yf

from .empresa import analizar_empresa
from .finanzas import calcular_crecimientos
from .fmp import FCFEntry, FMPClientError, obtener_fcf_historico


def _obtener_fcf_yfinance(ticker: str, limite: int = 5) -> List[float]:
    """Devuelve los valores históricos de FCF usando yfinance."""
    empresa_temp = yf.Ticker(ticker)
    cashflow_temp = getattr(empresa_temp, "cashflow", None)

    if cashflow_temp is None or cashflow_temp.empty or "Free Cash Flow" not in cashflow_temp.index:
        return []

    serie = cashflow_temp.loc["Free Cash Flow"].dropna().head(limite)
    valores: List[float] = []
    if hasattr(serie, "tolist"):
        iterador = serie.tolist()
    else:
        iterador = serie

    for bruto in iterador:
        try:
            valores.append(float(bruto))
        except (TypeError, ValueError):
            continue

    return valores


def ejecutar_dcf(ticker: str, metodo: str = "1", fuente: str = "auto") -> dict:
    """
    Ejecuta el análisis DCF para un ticker dado y devuelve un diccionario con los resultados clave.

    Args:
        ticker (str): Símbolo bursátil de la empresa.
        metodo (str): "1" para usar CAGR, "2" para promedio año a año. Default: "1".

    Returns:
        dict: Contiene 'precio_actual', 'valor_intrinseco', 'estado', 'diferencia_pct'.
    """
    fuente_solicitada = (fuente or "auto").lower()
    fcf_historial: List[FCFEntry] = []
    valores_para_crecimiento: List[float] = []
    fuente_utilizada = "yfinance"
    mensaje_fuente = None
    fmp_error: str | None = None

    usar_fmp = fuente_solicitada in ("auto", "fmp")

    if usar_fmp:
        try:
            fcf_historial = obtener_fcf_historico(ticker, minimo=4, limite=5)
        except FMPClientError as exc:
            fmp_error = str(exc)
        else:
            if fcf_historial:
                valores_para_crecimiento = [dato.value for dato in fcf_historial]
                fuente_utilizada = "fmp"

    if fuente_utilizada != "fmp":
        valores_para_crecimiento = _obtener_fcf_yfinance(ticker, limite=5)
        fuente_utilizada = "yfinance"

        if usar_fmp:
            if fuente_solicitada == "fmp":
                if fmp_error:
                    mensaje_fuente = (
                        "No se pudieron obtener datos desde Financial Modeling Prep "
                        f"({fmp_error}). Se utilizó iFinance."
                    )
                else:
                    mensaje_fuente = (
                        "Financial Modeling Prep no devolvió datos para este ticker. "
                        "Se utilizó iFinance."
                    )
            elif fuente_solicitada == "auto":
                if fmp_error:
                    mensaje_fuente = (
                        "Se utilizó iFinance porque Financial Modeling Prep devolvió un error "
                        f"({fmp_error})."
                    )
                else:
                    mensaje_fuente = (
                        "Se utilizó iFinance porque Financial Modeling Prep no tiene datos para este ticker."
                    )

    crecimiento, avg_growth_rate = calcular_crecimientos(valores_para_crecimiento)

    resultado = analizar_empresa(
        ticker,
        metodo,
        crecimiento,
        avg_growth_rate,
        fcf_historial=fcf_historial if fuente_utilizada == "fmp" else None
    )

    descripcion_fuentes = {
        "fmp": "Financial Modeling Prep",
        "yfinance": "iFinance (yfinance)",
    }

    resultado["fuente_datos"] = fuente_utilizada
    resultado["fuente_datos_descripcion"] = descripcion_fuentes.get(
        fuente_utilizada, fuente_utilizada
    )
    resultado["fuente_solicitada"] = fuente_solicitada
    if mensaje_fuente:
        resultado["mensaje_fuente"] = mensaje_fuente

    return resultado
