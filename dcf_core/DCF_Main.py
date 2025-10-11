from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from .empresa import analizar_empresa
from .finanzas import calcular_crecimientos
from .fmp import (
    FCFEntry,
    FMPClientError,
    FMPDerivedMetrics,
    obtener_crecimiento_analistas,
    obtener_fcf_historico,
    obtener_metricas_financieras,
)


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


def _obtener_metricas_yfinance(ticker: str, limite: int = 5) -> tuple[Optional[float], Dict[int, float], Optional[float], Dict[int, float]]:
    """Calcula métricas de tasa y costo de deuda usando yfinance."""

    empresa = yf.Ticker(ticker)
    financials = getattr(empresa, "financials", None)
    balance = getattr(empresa, "balance_sheet", None)

    tasas: Dict[int, float] = {}
    costos: Dict[int, float] = {}

    if financials is not None and not financials.empty:
        if "Income Tax Expense" in financials.index and "Income Before Tax" in financials.index:
            impuestos = financials.loc["Income Tax Expense"].dropna()
            ingreso_pre = financials.loc["Income Before Tax"].dropna()
            for fecha, impuesto in impuestos.items():
                if fecha not in ingreso_pre:
                    continue
                pre_impuesto = ingreso_pre[fecha]
                try:
                    impuesto_float = abs(float(impuesto))
                    pre_float = float(pre_impuesto)
                except (TypeError, ValueError):
                    continue
                if pre_float == 0:
                    continue
                tasa = impuesto_float / abs(pre_float)
                if 0 <= tasa < 1.5:
                    año = getattr(fecha, "year", None)
                    if año is None:
                        try:
                            año = int(str(fecha)[:4])
                        except (TypeError, ValueError):
                            continue
                    tasas[año] = tasa

        interes_series = None
        if "Interest Expense" in financials.index:
            interes_series = financials.loc["Interest Expense"].dropna()
        elif "Interest Expense Non Operating" in financials.index:
            interes_series = financials.loc["Interest Expense Non Operating"].dropna()
    else:
        interes_series = None

    deuda_por_año: Dict[int, float] = {}
    if balance is not None and not balance.empty:
        total_debt_row = balance.loc["Total Debt"] if "Total Debt" in balance.index else None
        short_debt_row = balance.loc["Short Long Term Debt"] if "Short Long Term Debt" in balance.index else None
        long_debt_row = balance.loc["Long Term Debt"] if "Long Term Debt" in balance.index else None

        for fecha in balance.columns:
            deuda = None
            if total_debt_row is not None and fecha in total_debt_row and not pd.isna(total_debt_row[fecha]):
                try:
                    deuda = abs(float(total_debt_row[fecha]))
                except (TypeError, ValueError):
                    deuda = None
            else:
                suma = 0.0
                encontrado = False
                for fila in (short_debt_row, long_debt_row):
                    if fila is None or fecha not in fila or pd.isna(fila[fecha]):
                        continue
                    try:
                        suma += abs(float(fila[fecha]))
                        encontrado = True
                    except (TypeError, ValueError):
                        continue
                if encontrado:
                    deuda = suma

            if not deuda:
                continue
            año = getattr(fecha, "year", None)
            if año is None:
                try:
                    año = int(str(fecha)[:4])
                except (TypeError, ValueError):
                    continue
            deuda_por_año[año] = deuda

    if interes_series is not None:
        for fecha, interes in interes_series.items():
            año = getattr(fecha, "year", None)
            if año is None:
                try:
                    año = int(str(fecha)[:4])
                except (TypeError, ValueError):
                    continue
            deuda = deuda_por_año.get(año)
            if not deuda:
                continue
            try:
                interes_float = abs(float(interes))
            except (TypeError, ValueError):
                continue
            costo = interes_float / deuda
            if costo >= 0:
                costos[año] = costo

    tasa_promedio = sum(tasas.values()) / len(tasas) if tasas else None
    costo_promedio = sum(costos.values()) / len(costos) if costos else None

    return tasa_promedio, tasas, costo_promedio, costos


def ejecutar_dcf(
    ticker: str,
    metodo: str = "1",
    fuente: str = "auto",
    growth_override: Optional[float] = None,
) -> dict:
    """
    Ejecuta el análisis DCF para un ticker dado y devuelve un diccionario con los resultados clave.

    Args:
        ticker (str): Símbolo bursátil de la empresa.
        metodo (str): "1" para usar CAGR, "2" para promedio año a año. Default: "1".
        growth_override (float, opcional): Permite forzar una tasa de crecimiento específica
            cuando el método seleccionado es CAGR.

    Returns:
        dict: Contiene 'precio_actual', 'valor_intrinseco', 'estado', 'diferencia_pct'.
    """
    fuente_solicitada = (fuente or "auto").lower()
    fcf_historial: List[FCFEntry] = []
    valores_para_crecimiento: List[float] = []
    fuente_utilizada = "yfinance"
    mensajes_fuente: List[str] = []
    fmp_error: str | None = None
    metricas_fuente: Dict[str, dict] = {}

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
                    mensajes_fuente.append(
                        "No se pudieron obtener datos desde Financial Modeling Prep "
                        f"({fmp_error}). Se utilizó Yfinance."
                    )
                else:
                    mensajes_fuente.append(
                        "Financial Modeling Prep no devolvió datos para este ticker. "
                        "Se utilizó Yfinance."
                    )
            elif fuente_solicitada == "auto":
                if fmp_error:
                    mensajes_fuente.append(
                        "Se utilizó Yfinance porque Financial Modeling Prep devolvió un error "
                        f"({fmp_error})."
                    )
                else:
                    mensajes_fuente.append(
                        "Se utilizó Yfinance porque Financial Modeling Prep no tiene datos para este ticker."
                    )

    tax_rate_override: Optional[float] = None
    cost_of_debt_override: Optional[float] = None

    metricas_fmp: Optional[FMPDerivedMetrics] = None
    fmp_metricas_error: str | None = None

    if usar_fmp:
        try:
            metricas_fmp = obtener_metricas_financieras(ticker, limite=5)
        except FMPClientError as exc:
            fmp_metricas_error = str(exc)

    if metricas_fmp:
        if metricas_fmp.tax_rate is not None:
            tax_rate_override = metricas_fmp.tax_rate
            metricas_fuente["tax_rate"] = {
                "fuente": "fmp",
                "muestras": metricas_fmp.tax_samples,
                "años": len(metricas_fmp.tax_samples),
            }
        if metricas_fmp.cost_of_debt is not None:
            cost_of_debt_override = metricas_fmp.cost_of_debt
            metricas_fuente["cost_of_debt"] = {
                "fuente": "fmp",
                "muestras": metricas_fmp.cost_samples,
                "años": len(metricas_fmp.cost_samples),
            }

    metricas_yf: Optional[tuple[Optional[float], Dict[int, float], Optional[float], Dict[int, float]]] = None

    if tax_rate_override is None or cost_of_debt_override is None:
        metricas_yf = _obtener_metricas_yfinance(ticker, limite=5)
        tasa_yf, tasas_yf, costo_yf, costos_yf = metricas_yf

        if tax_rate_override is None and tasa_yf is not None:
            tax_rate_override = tasa_yf
            metricas_fuente["tax_rate"] = {
                "fuente": "yfinance",
                "muestras": tasas_yf,
                "años": len(tasas_yf),
            }
        if cost_of_debt_override is None and costo_yf is not None:
            cost_of_debt_override = costo_yf
            metricas_fuente["cost_of_debt"] = {
                "fuente": "yfinance",
                "muestras": costos_yf,
                "años": len(costos_yf),
            }

    if tax_rate_override is None and metricas_fmp and metricas_fmp.tax_samples:
        mensajes_fuente.append(
            "No se pudo calcular una tasa impositiva válida con Financial Modeling Prep; se utilizaron los valores predeterminados."
        )
    if cost_of_debt_override is None and metricas_fmp and metricas_fmp.cost_samples:
        mensajes_fuente.append(
            "No se pudo calcular el costo de la deuda con los datos disponibles; se utilizaron los valores predeterminados."
        )
    if tax_rate_override is None and metricas_yf and metricas_yf[1]:
        mensajes_fuente.append(
            "No se obtuvo una tasa impositiva confiable; se usó el valor predeterminado del 25%."
        )
    if cost_of_debt_override is None and metricas_yf and metricas_yf[3]:
        mensajes_fuente.append(
            "No se obtuvo un costo de deuda confiable; se usó el valor predeterminado del 5%."
        )

    if fmp_metricas_error and fuente_solicitada == "fmp":
        mensajes_fuente.append(
            "No se pudieron recuperar los estados financieros de Financial Modeling Prep para calcular impuestos/deuda."
        )

    etiquetas_fuente = {
        "fmp": "Financial Modeling Prep",
        "yfinance": "Yfinance",
    }

    for info in metricas_fuente.values():
        fuente_id = info.get("fuente", "")
        if "descripcion" not in info:
            info["descripcion"] = etiquetas_fuente.get(fuente_id, fuente_id or "Fuente desconocida")

    for clave, texto in (("tax_rate", "Tasa impositiva"), ("cost_of_debt", "Costo de la deuda")):
        info = metricas_fuente.get(clave)
        if not info:
            continue
        fuente_id = info.get("fuente", "")
        etiqueta = info.get("descripcion") or etiquetas_fuente.get(fuente_id, fuente_id or "Fuente desconocida")
        años = info.get("años") or len(info.get("muestras", {}))
        if años:
            mensajes_fuente.append(f"{texto} calculado con {etiqueta} (datos de {años} años).")
        else:
            mensajes_fuente.append(f"{texto} calculado con {etiqueta}.")

    crecimiento, avg_growth_rate = calcular_crecimientos(valores_para_crecimiento)
    crecimiento_detectado = crecimiento

    crecimiento_analistas: Optional[float] = None
    if usar_fmp:
        try:
            crecimiento_analistas = obtener_crecimiento_analistas(ticker, limite=4)
        except FMPClientError:
            mensajes_fuente.append(
                "No se pudieron obtener las estimaciones de crecimiento provistas por analistas."
            )
        except Exception:  # pragma: no cover - dependiente de red
            mensajes_fuente.append(
                "Ocurrió un error inesperado al consultar las estimaciones de crecimiento de analistas."
            )

    crecimiento_aplicado = crecimiento
    if metodo == "1" and growth_override is not None:
        try:
            crecimiento_aplicado = float(growth_override)
        except (TypeError, ValueError):
            crecimiento_aplicado = crecimiento

    resultado = analizar_empresa(
        ticker,
        metodo,
        crecimiento_aplicado,
        avg_growth_rate,
        fcf_historial=fcf_historial if fuente_utilizada == "fmp" else None,
        tax_rate_override=tax_rate_override,
        cost_of_debt_override=cost_of_debt_override,
        metricas_fuente=metricas_fuente,
    )

    descripcion_fuentes = {
        "fmp": "Financial Modeling Prep",
        "yfinance": "Yfinance",
    }

    resultado["fuente_datos"] = fuente_utilizada
    resultado["fuente_datos_descripcion"] = descripcion_fuentes.get(
        fuente_utilizada, fuente_utilizada
    )
    resultado["fuente_solicitada"] = fuente_solicitada
    resultado["crecimiento_detectado"] = crecimiento_detectado
    resultado["crecimiento_detectado_pct"] = (
        crecimiento_detectado * 100 if crecimiento_detectado is not None else None
    )
    resultado["crecimiento_utilizado"] = crecimiento_aplicado
    resultado["crecimiento_utilizado_pct"] = (
        crecimiento_aplicado * 100 if crecimiento_aplicado is not None else None
    )
    resultado["crecimiento_promedio_pct"] = (
        avg_growth_rate * 100 if avg_growth_rate is not None else None
    )
    resultado["crecimiento_analistas"] = crecimiento_analistas
    resultado["crecimiento_analistas_pct"] = (
        crecimiento_analistas * 100 if crecimiento_analistas is not None else None
    )
    if metodo == "1" and growth_override is not None:
        resultado["growth_override_aplicado"] = crecimiento_aplicado

    if mensajes_fuente:
        resultado["mensaje_fuente"] = " ".join(mensajes_fuente)

    return resultado
