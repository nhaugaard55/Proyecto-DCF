from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from .empresa import analizar_empresa, _fetch_news, _generate_ai_summary, _primer_periodo_es_parcial
from .finanzas import calcular_crecimientos, calcular_escenarios, calcular_tabla_sensibilidad
from .fmp import (
    FCFEntry,
    FMPClientError,
    FMPDerivedMetrics,
    obtener_fcf_historico,
    obtener_metricas_financieras,
)

_PREFETCH_TIMEOUT = 20  # segundos máximos por tarea


def _prefetch_concurrent(
    ticker: str, empresa_yf: yf.Ticker
) -> tuple[List[FCFEntry], Optional[str], Optional[FMPDerivedMetrics], Optional[str]]:
    """
    Lanza en paralelo todas las llamadas externas necesarias para el análisis:
      - yfinance: info, cashflow, financials, balance_sheet, history(5y)
      - FMP: FCF histórico + métricas financieras

    Las propiedades de yfinance quedan cacheadas en el objeto, por lo que
    los accesos posteriores son instantáneos.

    Devuelve (fcf_historial, fmp_fcf_error, metricas_fmp, fmp_metricas_error).
    """
    fcf_historial: List[FCFEntry] = []
    fmp_fcf_error: Optional[str] = None
    metricas_fmp: Optional[FMPDerivedMetrics] = None
    fmp_metricas_error: Optional[str] = None

    def _yf_info():
        try:
            _ = empresa_yf.info
        except Exception:
            pass

    def _yf_cashflow():
        try:
            _ = empresa_yf.cashflow
        except Exception:
            pass

    def _yf_financials():
        try:
            _ = empresa_yf.financials
        except Exception:
            pass

    def _yf_balance():
        try:
            _ = empresa_yf.balance_sheet
        except Exception:
            pass

    def _yf_history_5y():
        try:
            _ = empresa_yf.history(period="5y")
        except Exception:
            pass

    def _yf_history_1y():
        try:
            _ = empresa_yf.history(period="1y")
        except Exception:
            pass

    def _yf_history_1d():
        try:
            _ = empresa_yf.history(period="1d")
        except Exception:
            pass

    def _yf_news():
        try:
            _ = empresa_yf.news
        except Exception:
            pass

    def _fmp_fcf():
        nonlocal fcf_historial, fmp_fcf_error
        try:
            fcf_historial = obtener_fcf_historico(ticker, minimo=4, limite=5)
        except FMPClientError as exc:
            fmp_fcf_error = str(exc)
        except Exception as exc:
            fmp_fcf_error = str(exc)

    def _fmp_metricas():
        nonlocal metricas_fmp, fmp_metricas_error
        try:
            metricas_fmp = obtener_metricas_financieras(ticker, limite=5)
        except FMPClientError as exc:
            fmp_metricas_error = str(exc)
        except Exception as exc:
            fmp_metricas_error = str(exc)

    tasks = [_yf_info, _yf_cashflow, _yf_financials, _yf_balance,
             _yf_history_5y, _yf_history_1y, _yf_history_1d, _yf_news,
             _fmp_fcf, _fmp_metricas]

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): fn.__name__ for fn in tasks}
        for future in as_completed(futures, timeout=_PREFETCH_TIMEOUT):
            try:
                future.result()
            except Exception:
                pass

    return fcf_historial, fmp_fcf_error, metricas_fmp, fmp_metricas_error


def _obtener_fcf_yfinance(ticker: str, empresa_yf: yf.Ticker, limite: int = 5) -> List[float]:
    """Devuelve los valores históricos de FCF usando yfinance."""
    cashflow_temp = getattr(empresa_yf, "cashflow", None)

    if cashflow_temp is None or cashflow_temp.empty or "Free Cash Flow" not in cashflow_temp.index:
        return []

    serie = cashflow_temp.loc["Free Cash Flow"].dropna()
    # Excluir el primer período si es un año fiscal incompleto (stub del año en curso).
    # Un gap < 300 días entre los dos primeros períodos anuales indica un stub parcial.
    if _primer_periodo_es_parcial(cashflow_temp) and len(serie) > 1:
        serie = serie.iloc[1:]
    serie = serie.head(limite)

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


def _obtener_metricas_yfinance(ticker: str, empresa_yf: yf.Ticker, limite: int = 5) -> tuple[Optional[float], Dict[int, float], Optional[float], Dict[int, float]]:
    """Calcula métricas de tasa y costo de deuda usando yfinance."""
    import logging
    logger = logging.getLogger(__name__)

    from .finanzas import obtener_tasa_libre_riesgo
    try:
        rf = obtener_tasa_libre_riesgo()
    except Exception:
        rf = 0.0441

    financials = getattr(empresa_yf, "financials", None)
    balance = getattr(empresa_yf, "balance_sheet", None)

    tasas: Dict[int, float] = {}
    costos: Dict[int, float] = {}

    interes_series = None
    ebit_series = None

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

        if "Interest Expense" in financials.index:
            interes_series = financials.loc["Interest Expense"].dropna()
        elif "Interest Expense Non Operating" in financials.index:
            interes_series = financials.loc["Interest Expense Non Operating"].dropna()

        # EBIT for interest coverage (used by Kd floor logic)
        for _ebit_label in ("EBIT", "Operating Income"):
            if _ebit_label in financials.index:
                ebit_series = financials.loc[_ebit_label].dropna()
                break
    else:
        interes_series = None

    # Revenue for financial-arm detection heuristic
    revenue_ttm: Optional[float] = None
    try:
        income_stmt = getattr(empresa_yf, "income_stmt", None) or getattr(empresa_yf, "financials", None)
        if income_stmt is not None and not income_stmt.empty:
            for _rev_label in ("Total Revenue", "Revenue"):
                if _rev_label in income_stmt.index:
                    _rev_s = income_stmt.loc[_rev_label].dropna()
                    if not _rev_s.empty:
                        try:
                            revenue_ttm = abs(float(_rev_s.iloc[0]))
                        except (TypeError, ValueError):
                            pass
                        break
    except Exception:
        pass

    deuda_por_año: Dict[int, float] = {}
    if balance is not None and not balance.empty:
        # Use financial debt only: Total Debt (=LT+ST) preferred, fallback LT+ST separate rows
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

    # Build EBIT by year for interest coverage
    ebit_por_año: Dict[int, float] = {}
    if ebit_series is not None:
        for fecha, ebit_v in ebit_series.items():
            año = getattr(fecha, "year", None)
            if año is None:
                try:
                    año = int(str(fecha)[:4])
                except (TypeError, ValueError):
                    continue
            try:
                ebit_por_año[año] = float(ebit_v)
            except (TypeError, ValueError):
                continue

    # Total debt (most recent) for financial-arm heuristic
    total_debt_ttm: Optional[float] = None
    if deuda_por_año:
        total_debt_ttm = deuda_por_año[max(deuda_por_año.keys())]

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

            # Apply floor: Kd must not be below Rf
            ebit_año = {año: ebit_por_año[año]} if año in ebit_por_año else None
            from .fmp import _calcular_kd_con_floor
            kd = _calcular_kd_con_floor(
                interes_float=interes_float,
                deuda_financiera=deuda,
                rf=rf,
                ebit_por_año=ebit_año,
                revenue_total=revenue_ttm,
                total_debt_total=total_debt_ttm,
            )
            if kd is not None and kd >= 0:
                costos[año] = kd

    tasa_promedio = sum(tasas.values()) / len(tasas) if tasas else None
    costo_promedio = sum(costos.values()) / len(costos) if costos else None

    return tasa_promedio, tasas, costo_promedio, costos


def ejecutar_dcf(ticker: str, metodo: str = "auto", fuente: str = "auto") -> dict:
    """
    Ejecuta el análisis DCF para un ticker dado y devuelve un diccionario con los resultados clave.

    Args:
        ticker (str): Símbolo bursátil de la empresa.
        metodo (str): Se conserva por compatibilidad, pero el análisis elige
            automáticamente la tasa de crecimiento más cercana a cero.

    Returns:
        dict: Contiene 'precio_actual', 'valor_intrinseco', 'estado', 'diferencia_pct'.
    """
    fuente_solicitada = "auto"
    fcf_historial: List[FCFEntry] = []
    valores_para_crecimiento: List[float] = []
    fuente_utilizada = "yfinance"
    mensajes_fuente: List[str] = []
    fmp_error: str | None = None
    metricas_fuente: Dict[str, dict] = {}

    empresa_yf = yf.Ticker(ticker)

    # ── Pre-fetch en paralelo: yfinance × 5 + FMP × 2 ──────────
    fcf_historial, fmp_error, metricas_fmp, fmp_metricas_error = _prefetch_concurrent(
        ticker, empresa_yf
    )

    # ── Determinar fuente de FCF ─────────────────────────────────
    if fcf_historial:
        # Excluir el año fiscal en curso si FMP lo incluyó como entrada parcial.
        # year >= año_actual → año fiscal que aún no cerró (o que abrió este año).
        # El CAGR debe calcularse solo sobre años completos.
        _año_actual_dcf = pd.Timestamp.now().year
        valores_para_crecimiento = [
            dato.value for dato in fcf_historial
            if dato.year is None or dato.year < _año_actual_dcf
        ]
        fuente_utilizada = "fmp"

    if fuente_utilizada != "fmp":
        valores_para_crecimiento = _obtener_fcf_yfinance(ticker, empresa_yf, limite=5)
        fuente_utilizada = "yfinance"

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

    nota_estructura_capital_fmp: Optional[str] = None

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
        nota_estructura_capital_fmp = getattr(metricas_fmp, "nota_estructura_capital", None)

    metricas_yf: Optional[tuple[Optional[float], Dict[int, float], Optional[float], Dict[int, float]]] = None

    if tax_rate_override is None or cost_of_debt_override is None:
        metricas_yf = _obtener_metricas_yfinance(ticker, empresa_yf, limite=5)
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

    if fmp_metricas_error:
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

    # ── Paralelizar DCF + noticias/IA ───────────────────────────
    # analizar_empresa corre sin noticias (skip_news=True) mientras
    # el pipeline news→IA corre en un thread separado. Se unen al final.
    nombre_empresa = (getattr(empresa_yf, "info", {}) or {}).get("shortName", ticker)

    def _pipeline_noticias():
        noticias, fuentes, error = _fetch_news(ticker, empresa_yf, nombre_empresa)
        resumen, resumen_error = _generate_ai_summary(noticias, ticker, nombre_empresa)
        return noticias, fuentes, error, resumen, resumen_error

    with ThreadPoolExecutor(max_workers=2) as _ex:
        _f_noticias = _ex.submit(_pipeline_noticias)
        _f_dcf = _ex.submit(
            analizar_empresa,
            ticker, "auto", crecimiento, avg_growth_rate,
            fcf_historial if fuente_utilizada == "fmp" else None,
            tax_rate_override, cost_of_debt_override, metricas_fuente, empresa_yf,
            True,  # skip_news
        )
        resultado = _f_dcf.result()
        noticias_data = _f_noticias.result()

    # Inyectar nota de estructura de capital (brazo financiero) si aplica
    if nota_estructura_capital_fmp:
        resultado["nota_estructura_capital"] = nota_estructura_capital_fmp

    # Inyectar noticias en el resultado
    _noticias, _fuentes, _n_error, _resumen, _r_error = noticias_data
    mapa_fuentes = {"marketaux": "Marketaux", "finnhub": "Finnhub", "yfinance": "YFinance"}
    fuentes_detectadas = [mapa_fuentes.get(f, f.title()) for f in sorted(_fuentes)]
    resultado["noticias"] = _noticias
    resultado["noticias_fuente"] = ",".join(sorted(_fuentes)) if _fuentes else None
    resultado["noticias_error"] = _n_error
    resultado["noticias_fuente_descripcion"] = ", ".join(fuentes_detectadas) if fuentes_detectadas else None
    resultado["resumen_noticias"] = _resumen
    resultado["resumen_noticias_error"] = _r_error

    descripcion_fuentes = {
        "fmp": "Financial Modeling Prep",
        "yfinance": "Yfinance",
    }
    crecimiento_base_val = (resultado.get("metricas", {}) or {}).get("crecimiento", crecimiento)

    # --- Escenarios bull/base/bear ---
    try:
        precio = resultado.get("precio_actual") or 0.0
        fcf_actual_val = valores_para_crecimiento[0] if valores_para_crecimiento else 0.0
        datos_empresa = resultado.get("datos_empresa", {})
        deuda_neta_val = datos_empresa.get("deuda_neta", 0.0) or 0.0
        acciones_val = datos_empresa.get("acciones", 0.0) or 0.0
        wacc_val = (resultado.get("metricas", {}) or {}).get("wacc", 0.08) or 0.08
        escenarios = calcular_escenarios(
            fcf_actual_val, crecimiento_base_val, wacc_val, deuda_neta_val, acciones_val, precio
        )
        resultado["escenarios"] = escenarios
    except Exception:
        resultado["escenarios"] = None

    # --- Tabla de sensibilidad ---
    try:
        tabla = calcular_tabla_sensibilidad(
            fcf_actual_val, wacc_val, crecimiento_base_val, deuda_neta_val, acciones_val, precio
        )
        resultado["tabla_sensibilidad"] = tabla
    except Exception:
        resultado["tabla_sensibilidad"] = None

    # --- Historial de precios (5 años) para gráfico ---
    try:
        hist = empresa_yf.history(period="5y")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            hist_clean = hist["Close"].dropna()
            resultado["precio_historico"] = {
                "fechas": [d.strftime("%Y-%m-%d") for d in hist_clean.index],
                "precios": [round(float(v), 2) for v in hist_clean.values],
            }
        else:
            resultado["precio_historico"] = None
    except Exception:
        resultado["precio_historico"] = None

    resultado["fuente_datos"] = fuente_utilizada
    resultado["fuente_datos_descripcion"] = descripcion_fuentes.get(
        fuente_utilizada, fuente_utilizada
    )
    resultado["fuente_solicitada"] = fuente_solicitada
    if mensajes_fuente:
        resultado["mensaje_fuente"] = " ".join(mensajes_fuente)

    return resultado
