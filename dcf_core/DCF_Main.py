from typing import List

from .empresa import analizar_empresa
from .finanzas import calcular_crecimientos
from .fmp import FCFEntry, obtener_fcf_historico


def ejecutar_dcf(ticker: str, metodo: str = "1") -> dict:
    """
    Ejecuta el análisis DCF para un ticker dado y devuelve un diccionario con los resultados clave.

    Args:
        ticker (str): Símbolo bursátil de la empresa.
        metodo (str): "1" para usar CAGR, "2" para promedio año a año. Default: "1".

    Returns:
        dict: Contiene 'precio_actual', 'valor_intrinseco', 'estado', 'diferencia_pct'.
    """
    fcf_historial: List[FCFEntry] = obtener_fcf_historico(ticker, minimo=6, limite=12)
    valores_para_crecimiento = [dato.value for dato in fcf_historial[:7]]
    crecimiento, avg_growth_rate = calcular_crecimientos(valores_para_crecimiento)

    return analizar_empresa(
        ticker,
        metodo,
        crecimiento,
        avg_growth_rate,
        fcf_historial=fcf_historial
    )
