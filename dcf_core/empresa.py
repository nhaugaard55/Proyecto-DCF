import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

import yfinance as yf

from .ai_summary import AISummaryError, generar_analisis_sentimiento
from .finanzas import (
    G_TERMINAL,
    obtener_tasa_libre_riesgo_con_fuente,
    calcular_wacc,
    proyectar_fcf,
    calcular_valor_intrinseco,
    seleccionar_metodo_crecimiento,
)

from .marketaux import MarketauxError, obtener_noticias_marketaux
from .finnhub import FinnhubError, obtener_noticias_finnhub
from .fmp import FCFEntry, obtener_sector_empresa, obtener_shares_diluidas_fmp
from .utils import parse_datetime_epoch

MAX_NEWS_ITEMS = 18


# ---------------------------------------------------------------------------
# Helpers de conversión (nivel de módulo para reutilización)
# ---------------------------------------------------------------------------

def to_float(value, default=0.0) -> float:
    if isinstance(value, complex):
        value = value.real
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def to_billions(value) -> Optional[float]:
    if isinstance(value, complex):
        value = value.real
    try:
        return float(value) / 1_000_000_000 if value is not None else None
    except (TypeError, ValueError):
        return None


def smart_format_billions(value) -> Optional[str]:
    """Formats monetary value as $X.XXM when < $10M, else $X.XXB. Returns None when no data."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    abs_v = abs(v)
    if abs_v < 10_000_000:
        return f"${v / 1_000_000:.2f}M"
    return f"${v / 1_000_000_000:.2f}B"


def to_optional_float(value) -> Optional[float]:
    try:
        if isinstance(value, complex):
            value = value.real
        return float(value)
    except (TypeError, ValueError):
        return None


def _primer_periodo_es_parcial(df_anual) -> bool:
    """
    True si la primera columna (período más reciente) de un DataFrame
    anual de yfinance representa un año fiscal incompleto.

    yfinance a veces incluye un stub del año en curso como primer columna
    de .cashflow / .income_stmt cuando la empresa ya publicó 1-2 trimestres
    pero el ejercicio fiscal no cerró. El gap entre el primer y segundo
    período < 300 días (< 10 meses) es la señal inequívoca de un stub.
    """
    if df_anual is None or df_anual.empty or len(df_anual.columns) < 2:
        return False
    try:
        t0 = pd.Timestamp(df_anual.columns[0])
        t1 = pd.Timestamp(df_anual.columns[1])
        # Fecha futura → definitivamente incompleto
        if t0 > pd.Timestamp.now():
            return True
        # Gap < 300 días entre períodos sucesivos → stub parcial
        return (t0 - t1).days < 300
    except Exception:
        return False


def _fcf_ttm_trimestral(empresa_yf) -> Optional[float]:
    """
    Calcula el FCF TTM sumando los últimos 4 trimestres de quarterly_cashflow.
    Siempre correcto independientemente del estado del año fiscal en curso.
    Devuelve None si no hay exactamente 4 trimestres disponibles.
    """
    try:
        qcf = getattr(empresa_yf, "quarterly_cashflow", None)
        if qcf is None or qcf.empty or "Free Cash Flow" not in qcf.index:
            return None
        fcf_q = qcf.loc["Free Cash Flow"].dropna()
        if len(fcf_q) < 4:
            return None
        return float(fcf_q.head(4).sum())
    except Exception:
        return None


def normalizar_dividend_yield(
    raw_yield, raw_dividend_rate, current_price
) -> Optional[float]:
    valor = to_optional_float(raw_yield)
    if valor is not None:
        if valor > 5:
            valor = valor / 100.0
        if valor < 0:
            valor = None

    tasa = to_optional_float(raw_dividend_rate)
    precio_actual = to_optional_float(current_price)
    calculado = None
    if tasa is not None and precio_actual not in (None, 0):
        try:
            calculado = max(tasa / precio_actual, 0.0)
        except ZeroDivisionError:
            calculado = None

    if valor is None:
        return calculado

    if calculado is not None:
        limite_base = 0.1
        limite_superior = max(calculado * 4, limite_base)
        if valor > limite_superior:
            return calculado

    return valor


def _limpiar_mensaje_api(texto: str) -> str:
    """Oculta parámetros sensibles (como claves) dentro de mensajes de error."""
    if not texto:
        return texto
    texto = re.sub(r"apikey=[^&\s]+", "apikey=****", texto)
    texto = re.sub(r"token=[^&\s]+", "token=****", texto)
    return texto


def _menciona(
    texto: Optional[str],
    ticker_lower: str,
    nombre_normalizado: str,
    nombre_sin_inc: str,
    nombre_simple: str,
) -> bool:
    texto_busqueda = str(texto or "").lower()
    if not texto_busqueda.strip():
        return False
    if ticker_lower and ticker_lower in texto_busqueda:
        return True
    if nombre_normalizado and nombre_normalizado in texto_busqueda:
        return True
    if nombre_sin_inc and nombre_sin_inc.strip() and nombre_sin_inc in texto_busqueda:
        return True
    if nombre_simple:
        patron = fr"\b{re.escape(nombre_simple)}['']s\b"
        if re.search(patron, texto_busqueda):
            return True
    return False


# ---------------------------------------------------------------------------
# Obtención de noticias
# ---------------------------------------------------------------------------

def _fetch_news(
    ticker: str,
    empresa_yf: yf.Ticker,
    nombre: str,
) -> Tuple[List[dict], set, Optional[str]]:
    """Obtiene y deduplica noticias de Marketaux, Finnhub e yfinance."""

    noticias: List[dict] = []
    noticias_fuentes: set = set()
    noticias_error: Optional[str] = None

    # --- Marketaux ---
    try:
        noticias_marketaux = obtener_noticias_marketaux(ticker, limite=MAX_NEWS_ITEMS)
    except MarketauxError as exc:
        noticias_error = _limpiar_mensaje_api(str(exc))
        noticias_marketaux = []
    except Exception as exc:
        mensaje = f"No se pudieron obtener noticias desde Marketaux ({_limpiar_mensaje_api(str(exc))})."
        noticias_error = mensaje
        noticias_marketaux = []

    if noticias_marketaux:
        noticias_fuentes.add("marketaux")
        for noticia in noticias_marketaux:
            noticias.append({
                "titulo": noticia.title,
                "fuente": noticia.source,
                "resumen": noticia.summary,
                "url": noticia.url,
                "imagen": noticia.image,
                "fecha": noticia.published_at,
            })

    # --- Finnhub ---
    if len(noticias) < MAX_NEWS_ITEMS:
        restante = MAX_NEWS_ITEMS - len(noticias)
        try:
            noticias_finnhub = obtener_noticias_finnhub(ticker, limite=restante)
        except FinnhubError as exc:
            mensaje = _limpiar_mensaje_api(str(exc))
            noticias_error = f"{noticias_error}. {mensaje}" if noticias_error else mensaje
            noticias_finnhub = []
        except Exception as exc:
            mensaje = f"No se pudieron obtener noticias desde Finnhub ({_limpiar_mensaje_api(str(exc))})."
            noticias_error = f"{noticias_error}. {mensaje}" if noticias_error else mensaje
            noticias_finnhub = []

        if noticias_finnhub:
            noticias_fuentes.add("finnhub")
            for noticia in noticias_finnhub:
                noticias.append({
                    "titulo": noticia.title,
                    "fuente": noticia.source,
                    "resumen": noticia.summary,
                    "url": noticia.url,
                    "imagen": noticia.image,
                    "fecha": noticia.published_at,
                })

    # --- yfinance ---
    yfinance_consultado = False
    if len(noticias) < MAX_NEWS_ITEMS:
        restante = MAX_NEWS_ITEMS - len(noticias)
        try:
            raw_news_any = getattr(empresa_yf, "news", None)
        except Exception as exc:
            raw_news = []
            mensaje = f"YFinance no devolvió noticias ({exc})"
            noticias_error = f"{noticias_error}. {mensaje}" if noticias_error else mensaje
        else:
            raw_news = list(raw_news_any or [])
        finally:
            yfinance_consultado = True

        if raw_news:
            noticias_fuentes.add("yfinance")
            for item in raw_news[:restante]:
                if not isinstance(item, dict):
                    continue
                titulo = (item.get("title") or item.get("headline") or "").strip()
                enlace = (item.get("link") or item.get("url") or "").strip()
                if not titulo or not enlace:
                    continue

                fuente = (item.get("publisher") or item.get("source") or "").strip() or None
                resumen = (item.get("summary") or item.get("content") or item.get("description") or None)
                if resumen:
                    resumen = resumen.strip() or None

                imagen = None
                thumbnail = item.get("thumbnail")
                if isinstance(thumbnail, dict):
                    url_directa = thumbnail.get("url")
                    if isinstance(url_directa, str) and url_directa.strip():
                        imagen = url_directa.strip()
                    else:
                        resoluciones = thumbnail.get("resolutions")
                        if isinstance(resoluciones, list):
                            for res in resoluciones:
                                url_res = (res.get("url") if isinstance(res, dict) else None)
                                if isinstance(url_res, str) and url_res.strip():
                                    imagen = url_res.strip()
                                    break

                marca_tiempo = (
                    item.get("providerPublishTime")
                    or item.get("providerPublishTimeUTC")
                    or item.get("datetime")
                )
                publicado = parse_datetime_epoch(marca_tiempo) if isinstance(marca_tiempo, (int, float)) else None

                noticias.append({
                    "titulo": titulo,
                    "fuente": fuente,
                    "resumen": resumen,
                    "url": enlace,
                    "imagen": imagen,
                    "fecha": publicado,
                })
        elif not noticias and yfinance_consultado and noticias_error is None:
            noticias_error = "YFinance no reportó noticias recientes para este ticker."

    # --- Deduplicar por URL ---
    noticias_por_url: dict[str, dict] = {}
    for item in noticias:
        url = item.get("url")
        if not url:
            continue
        existente = noticias_por_url.get(url)
        fecha_item = item.get("fecha")
        fecha_existente = existente.get("fecha") if existente else None
        if existente is None or (
            isinstance(fecha_item, datetime)
            and (not isinstance(fecha_existente, datetime) or fecha_item > fecha_existente)
        ):
            noticias_por_url[url] = item

    noticias = list(noticias_por_url.values())
    noticias.sort(
        key=lambda n: (
            n.get("fecha") is None,
            -(n.get("fecha").timestamp()) if isinstance(n.get("fecha"), datetime) else 0,
        )
    )
    noticias = noticias[:MAX_NEWS_ITEMS]

    if not noticias and noticias_error is None:
        noticias_error = "No se encontraron noticias recientes para este ticker."

    return noticias, noticias_fuentes, noticias_error


# ---------------------------------------------------------------------------
# Resumen con IA
# ---------------------------------------------------------------------------

def _generate_ai_summary(
    noticias: List[dict],
    ticker: str,
    nombre: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Filtra noticias relevantes y genera un resumen de sentimiento con IA."""

    if not noticias:
        return None, None

    ticker_lower = ticker.lower()
    nombre_normalizado = nombre.lower()
    nombre_sin_inc = nombre_normalizado.replace(" inc.", "").replace(" inc", "")
    nombre_simple = nombre.split(" ")[0].lower() if nombre else ""

    noticias_resumen = [dict(item, empresa=nombre) for item in noticias]

    relevantes_titulo = [
        n for n in noticias_resumen
        if _menciona(n.get("titulo"), ticker_lower, nombre_normalizado, nombre_sin_inc, nombre_simple)
    ]
    if relevantes_titulo:
        noticias_resumen = relevantes_titulo
    else:
        relevantes_contenido = [
            n for n in noticias_resumen
            if _menciona(n.get("titulo"), ticker_lower, nombre_normalizado, nombre_sin_inc, nombre_simple)
            or _menciona(n.get("resumen"), ticker_lower, nombre_normalizado, nombre_sin_inc, nombre_simple)
        ]
        if relevantes_contenido:
            noticias_resumen = relevantes_contenido

    resumen_noticias: Optional[dict] = None
    resumen_noticias_error: Optional[str] = None

    try:
        resumen_noticias = generar_analisis_sentimiento(noticias_resumen)
    except AISummaryError as exc:
        resumen_noticias_error = _limpiar_mensaje_api(str(exc))
    except Exception as exc:
        resumen_noticias_error = f"Error generando el resumen con IA ({_limpiar_mensaje_api(str(exc))})."

    return resumen_noticias, resumen_noticias_error


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Análisis técnico
# ---------------------------------------------------------------------------

def _calcular_rsi(close: "pd.Series", period: int = 14) -> Optional[float]:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    last_loss = avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = avg_gain.iloc[-1] / last_loss
    return round(100 - (100 / (1 + rs)), 2)


def calcular_analisis_tecnico(empresa_yf: yf.Ticker, precio_actual: float) -> dict:
    """Calcula indicadores técnicos básicos: SMAs y RSI."""
    try:
        hist = empresa_yf.history(period="1y")
    except Exception:
        hist = None

    if hist is None or hist.empty or "Close" not in hist.columns:
        return {"disponible": False}

    close = hist["Close"].dropna()
    n = len(close)

    def sma(period: int) -> Optional[float]:
        if n < period:
            return None
        val = close.rolling(period).mean().iloc[-1]
        return round(float(val), 2) if pd.notna(val) else None

    sma_20 = sma(20)
    sma_50 = sma(50)
    sma_200 = sma(200)
    rsi = _calcular_rsi(close)

    def vs_sma(sma_val: Optional[float]) -> Optional[str]:
        if sma_val is None or not precio_actual:
            return None
        return "encima" if precio_actual >= sma_val else "debajo"

    filtros_tecnicos = [
        {
            "nombre": "Precio vs SMA 200",
            "descripcion": "Precio sobre la media móvil de 200 días — tendencia de largo plazo",
            "valor": f"${sma_200:.2f}" if sma_200 else "N/D",
            "criterio": "Precio encima",
            "cumple": vs_sma(sma_200) == "encima",
            "disponible": sma_200 is not None,
        },
        {
            "nombre": "Precio vs SMA 50",
            "descripcion": "Precio sobre la media móvil de 50 días — tendencia de mediano plazo",
            "valor": f"${sma_50:.2f}" if sma_50 else "N/D",
            "criterio": "Precio encima",
            "cumple": vs_sma(sma_50) == "encima",
            "disponible": sma_50 is not None,
        },
        {
            "nombre": "Precio vs SMA 20",
            "descripcion": "Precio sobre la media móvil de 20 días — tendencia de corto plazo",
            "valor": f"${sma_20:.2f}" if sma_20 else "N/D",
            "criterio": "Precio encima",
            "cumple": vs_sma(sma_20) == "encima",
            "disponible": sma_20 is not None,
        },
        {
            "nombre": "RSI (14)",
            "descripcion": "Relative Strength Index — momentum del precio (30–70 es zona neutral)",
            "valor": f"{rsi:.1f}" if rsi is not None else "N/D",
            "criterio": "30 – 70",
            "cumple": rsi is not None and 30 <= rsi <= 70,
            "disponible": rsi is not None,
        },
    ]

    # Señal general de entrada
    encima_sma50 = vs_sma(sma_50) == "encima"
    encima_sma200 = vs_sma(sma_200) == "encima"
    rsi_ok = rsi is not None and 30 <= rsi <= 70
    rsi_sobrecomprado = rsi is not None and rsi > 70
    rsi_sobrevendido = rsi is not None and rsi < 30

    if rsi_sobrecomprado:
        senal = "sobrecomprado"
        senal_texto = "Sobrecomprado — esperar corrección"
    elif encima_sma50 and rsi_ok:
        senal = "buena"
        senal_texto = "Buen punto de entrada"
    elif not encima_sma200 and not rsi_sobrevendido:
        senal = "esperar"
        senal_texto = "Tendencia bajista — esperar recuperación"
    elif rsi_sobrevendido:
        senal = "oportunidad"
        senal_texto = "Posible oportunidad — precio sobrevendido"
    else:
        senal = "neutral"
        senal_texto = "Señal neutral — seguir monitoreando"

    return {
        "disponible": True,
        "sma_20": sma_20,
        "sma_50": sma_50,
        "sma_200": sma_200,
        "rsi": rsi,
        "precio_vs_sma20": vs_sma(sma_20),
        "precio_vs_sma50": vs_sma(sma_50),
        "precio_vs_sma200": vs_sma(sma_200),
        "senal": senal,
        "senal_texto": senal_texto,
        "filtros": filtros_tecnicos,
    }


def analizar_empresa(
    ticker,
    metodo_crecimiento="auto",
    crecimiento=0.05,
    avg_growth_rate=0.05,
    fcf_historial: Optional[Sequence[FCFEntry]] = None,
    tax_rate_override: Optional[float] = None,
    cost_of_debt_override: Optional[float] = None,
    metricas_fuente: Optional[Dict[str, dict]] = None,
    empresa_yf: Optional[yf.Ticker] = None,
    skip_news: bool = False,
):
    if empresa_yf is None:
        empresa_yf = yf.Ticker(ticker)

    info = getattr(empresa_yf, "info", {}) or {}
    history = empresa_yf.history(period="1d")

    nombre = info.get("longName", ticker)
    sector = info.get("sector") or ""
    if not sector:
        sector, _ = obtener_sector_empresa(ticker)
    if not sector:
        sector = "Desconocido"
    beta = to_float(info.get("beta"), 1.0)
    beta_aviso = None
    if beta <= 0:
        beta_aviso = (
            f"Beta negativo o cero detectado ({beta:.2f}) — usando 0.5 "
            "como valor mínimo para el cálculo del CAPM"
        )
        beta = 0.5
    tax_rate_info = to_float(info.get("effectiveTaxRate"), 0.25)
    cost_of_debt_info = to_float(info.get("yield"), 0.05)

    tax_rate = tax_rate_info if tax_rate_override is None else float(tax_rate_override)
    cost_of_debt = cost_of_debt_info if cost_of_debt_override is None else float(cost_of_debt_override)

    acciones = to_float(info.get("sharesOutstanding"), 0)
    precio = 0.0
    if not history.empty:
        precio = to_float(history["Close"].iloc[-1], 0)
    else:
        precio = to_float(info.get("currentPrice") or info.get("previousClose"), 0)

    acciones_ajuste_aviso = None

    # CORRECCIÓN 2 — Intentar shares diluidas totales desde FMP (incluye todas las clases)
    if acciones and ticker:
        try:
            shares_fmp = obtener_shares_diluidas_fmp(ticker)
            if shares_fmp and shares_fmp > acciones * 1.20:
                fmp_m = round(shares_fmp / 1e6, 1)
                yf_m = round(acciones / 1e6, 1)
                acciones_ajuste_aviso = (
                    f"Acciones ajustadas — FMP reporta {fmp_m}M acciones diluidas "
                    f"vs {yf_m}M de yfinance. Posible estructura multi-clase (ej: LILAK, GOOGL, BRK). "
                    f"Usando {fmp_m}M."
                )
                acciones = shares_fmp
        except Exception:
            pass

    # CORRECCIÓN 1 — Chequeo de consistencia mejorado: market_cap / precio como referencia
    market_cap_yf = to_float(info.get("marketCap"), 0)
    if acciones and precio and market_cap_yf:
        shares_implicitas = market_cap_yf / precio
        if shares_implicitas > 0:
            if acciones < shares_implicitas * 0.60:
                # Subestimación significativa (>40%): probable estructura multi-clase
                orig_m = round(acciones / 1e6, 1)
                impl_m = round(shares_implicitas / 1e6, 1)
                acciones_ajuste_aviso = (
                    f"Acciones ajustadas — posible estructura multi-clase (ej: LILAK, GOOGL, BRK): "
                    f"se reportaban {orig_m}M, market cap implica {impl_m}M. Usando {impl_m}M."
                )
                acciones = shares_implicitas
            elif acciones > shares_implicitas * 1.40:
                # Sobreestimación (raro): posible dato erróneo de yfinance
                orig_m = round(acciones / 1e6, 1)
                impl_m = round(shares_implicitas / 1e6, 1)
                acciones_ajuste_aviso = (
                    f"Acciones ajustadas — dato yfinance ({orig_m}M) excede en >40% "
                    f"lo implicado por market cap ({impl_m}M). Usando {impl_m}M."
                )
                acciones = shares_implicitas

    equity = acciones * precio

    balance = getattr(empresa_yf, "balance_sheet", None)
    debt = 0.0
    if balance is not None and not balance.empty and "Long Term Debt" in balance.index:
        deuda_series = balance.loc["Long Term Debt"].dropna()
        if not deuda_series.empty:
            debt = to_float(deuda_series.iloc[0], 0)
    cash = to_float(info.get("totalCash"), 0)
    if (not cash) and balance is not None and not balance.empty:
        for label in (
            "Cash And Cash Equivalents",
            "Cash Cash Equivalents And Short Term Investments",
            "Cash Equivalents",
        ):
            if label in balance.index:
                cash_series = balance.loc[label].dropna()
                if not cash_series.empty:
                    cash = to_float(cash_series.iloc[0], 0)
                    break
    # Chequeo de escala de caja: mismo problema que revenue en ADRs de moneda local
    if cash and equity > 0 and cash > equity * 5:
        cash = cash / 1000
    net_debt = debt - cash  # LT-only, mantenido por compatibilidad

    # Deuda total (LP + corriente) para cálculo correcto del equity value
    total_debt = debt
    current_debt = 0.0
    if balance is not None and not balance.empty:
        if "Total Debt" in balance.index:
            _td = balance.loc["Total Debt"].dropna()
            if not _td.empty:
                total_debt = to_float(_td.iloc[0], 0)
        elif "Short Long Term Debt" in balance.index:
            _cd = balance.loc["Short Long Term Debt"].dropna()
            if not _cd.empty:
                current_debt = to_float(_cd.iloc[0], 0)
                total_debt = debt + current_debt
    net_debt_total = total_debt - cash

    # --- Additional balance sheet fields for Liquidation Value and Altman Z-Score ---
    total_current_assets_val = None
    total_current_liabilities_val = None
    total_assets_val = None
    total_liab_val = None
    retained_earnings_val = None
    working_capital_val = None

    if balance is not None and not balance.empty:
        _bs_map = {
            "Current Assets":                          "ca",
            "Current Liabilities":                    "cl",
            "Total Assets":                            "ta",
            "Total Liabilities Net Minority Interest": "tl",
            "Retained Earnings":                       "re",
            "Working Capital":                         "wc",
        }
        for _label, _key in _bs_map.items():
            if _label in balance.index:
                _s = balance.loc[_label].dropna()
                if not _s.empty:
                    _v = to_optional_float(_s.iloc[0])
                    if _key == "ca":   total_current_assets_val = _v
                    elif _key == "cl": total_current_liabilities_val = _v
                    elif _key == "ta": total_assets_val = _v
                    elif _key == "tl": total_liab_val = _v
                    elif _key == "re": retained_earnings_val = _v
                    elif _key == "wc": working_capital_val = _v

    # Compute working capital if not directly available
    if working_capital_val is None and total_current_assets_val is not None and total_current_liabilities_val is not None:
        working_capital_val = total_current_assets_val - total_current_liabilities_val

    income_stmt = None
    try:
        income_stmt = getattr(empresa_yf, "income_stmt", None)
        if income_stmt is None or (income_stmt is not None and income_stmt.empty):
            income_stmt = getattr(empresa_yf, "financials", None)
    except Exception:
        income_stmt = None

    # Detectar si el primer período del income_stmt es un año fiscal parcial.
    # Esto ocurre cuando yfinance incluye un stub del año en curso (ej: 2026 con
    # solo Q1+Q2 reportados) junto a los años completos anteriores.
    _income_stmt_parcial = _primer_periodo_es_parcial(income_stmt)

    # EBIT from income statement
    ebit_val = None
    try:
        _stmt = income_stmt
        if _stmt is not None and not _stmt.empty:
            for _ebit_label in ("EBIT", "Operating Income"):
                if _ebit_label in _stmt.index:
                    _ebit_s = _stmt.loc[_ebit_label].dropna()
                    if not _ebit_s.empty:
                        ebit_val = to_optional_float(_ebit_s.iloc[0])
                        break
    except Exception:
        ebit_val = None

    # EBITDA — primary from yfinance info, fallback EBIT + D&A from cashflow
    ebitda_val = to_optional_float(info.get("ebitda"))
    if ebitda_val is None and ebit_val is not None:
        try:
            _cf = getattr(empresa_yf, "cashflow", None)
            if _cf is not None and not _cf.empty:
                for _da_label in ("Depreciation & Amortization", "Depreciation", "DepreciationAndAmortization"):
                    if _da_label in _cf.index:
                        _da_s = _cf.loc[_da_label].dropna()
                        if not _da_s.empty:
                            _da = to_optional_float(_da_s.iloc[0])
                            if _da is not None:
                                ebitda_val = ebit_val + abs(_da)
                            break
        except Exception:
            pass

    # EPS 5-year CAGR — needed by Schwab Intrinsic Value model
    eps_growth_5y: Optional[float] = None
    eps_growth_5y_fuente: Optional[str] = None
    try:
        _eps_stmt = income_stmt
        if _eps_stmt is not None and not _eps_stmt.empty:
            # Try EPS directly from income statement first
            for _eps_label in ("Diluted EPS", "Basic EPS"):
                if _eps_label in _eps_stmt.index:
                    _eps_s = _eps_stmt.loc[_eps_label].dropna()
                    if len(_eps_s) >= 2:
                        _eps_recent = to_optional_float(_eps_s.iloc[0])
                        _eps_oldest = to_optional_float(_eps_s.iloc[-1])
                        _n = len(_eps_s)
                        if (
                            _eps_recent is not None and _eps_oldest is not None
                            and _eps_oldest > 0 and _eps_recent > 0
                        ):
                            eps_growth_5y = (_eps_recent / _eps_oldest) ** (1 / _n) - 1
                            eps_growth_5y_fuente = f"{_eps_label} CAGR ({_n}a)"
                    break
            # Fallback: Net Income CAGR (proxy when share count is stable)
            if eps_growth_5y is None and "Net Income" in _eps_stmt.index:
                _ni_s = _eps_stmt.loc["Net Income"].dropna()
                if len(_ni_s) >= 2:
                    _ni_r = to_optional_float(_ni_s.iloc[0])
                    _ni_o = to_optional_float(_ni_s.iloc[-1])
                    _n = len(_ni_s)
                    if _ni_r is not None and _ni_o is not None and _ni_o > 0 and _ni_r > 0:
                        eps_growth_5y = (_ni_r / _ni_o) ** (1 / _n) - 1
                        eps_growth_5y_fuente = f"Net Income CAGR ({_n}a)"
    except Exception:
        eps_growth_5y = None
    # Final fallback: YoY earningsGrowth from info
    if eps_growth_5y is None:
        _eg = to_optional_float(info.get("earningsGrowth"))
        if _eg is not None:
            eps_growth_5y = _eg
            eps_growth_5y_fuente = "YoY earningsGrowth (yfinance)"

    fcf: list[float] = []
    fcf_presentacion: list[tuple[Optional[int], float]] = []
    _cashflow_parcial = False  # inicializar antes del if/else; se sobreescribe en el else
    if fcf_historial:
        for entrada in fcf_historial:
            raw_valor = getattr(entrada, "value", None)
            if raw_valor is None:
                continue
            valor = to_float(raw_valor, 0.0)
            fcf.append(valor)

            raw_year = getattr(entrada, "year", None)
            try:
                year = int(raw_year) if raw_year is not None else None
            except (TypeError, ValueError):
                year = None
            fcf_presentacion.append((year, valor))
    else:
        cashflow = getattr(empresa_yf, "cashflow", None)
        _cashflow_parcial = _primer_periodo_es_parcial(cashflow)
        if cashflow is not None and not cashflow.empty and "Free Cash Flow" in cashflow.index:
            _fcf_raw_series = cashflow.loc["Free Cash Flow"].dropna()
            # Si el primer período es un año fiscal incompleto, omitirlo de la serie
            # histórica anual para no contaminar el CAGR ni la clasificación de etapa.
            if _cashflow_parcial and len(_fcf_raw_series) > 1:
                _fcf_raw_series = _fcf_raw_series.iloc[1:]
            fcf_series = _fcf_raw_series.head(5)
            if hasattr(fcf_series, "tolist"):
                fcf = [to_float(valor) for valor in fcf_series.tolist()]
            elif isinstance(fcf_series, (list, tuple)):
                fcf = [to_float(valor) for valor in fcf_series]
        for valor in fcf:
            fcf_presentacion.append((None, valor))

    # FCF TTM correcto: preferir suma de últimos 4 trimestres (siempre exacto,
    # independiente del estado del año fiscal en curso).
    _fcf_ttm_q = _fcf_ttm_trimestral(empresa_yf)
    # fcf_actual es el punto de partida del DCF; usar TTM trimestral si disponible.
    # La serie histórica anual (fcf[]) sigue siendo usada para el CAGR.
    fcf_actual = _fcf_ttm_q if _fcf_ttm_q is not None else (fcf[0] if fcf else 0.0)

    pe_ratio_raw = info.get("trailingPE")
    pe_ratio = to_float(pe_ratio_raw) if pe_ratio_raw is not None else None

    tasa_rf, rf_fuente = obtener_tasa_libre_riesgo_con_fuente()
    market_return = 0.08

    metodo_codigo, metodo_nombre, tasa_auto = seleccionar_metodo_crecimiento(
        crecimiento, avg_growth_rate
    )
    tasa_crecimiento = tasa_auto
    metodo_utilizado = metodo_nombre

    # Cap del CAGR usado en el DCF: valores >50% casi siempre reflejan un año base
    # anómalo (FCF muy bajo), no crecimiento real sostenible a 5 años.
    _CAGR_CAP_DCF = 0.50
    cagr_cap_applied = False
    cagr_antes_cap = tasa_crecimiento
    if tasa_crecimiento is not None and tasa_crecimiento > _CAGR_CAP_DCF:
        tasa_crecimiento = _CAGR_CAP_DCF
        cagr_cap_applied = True

    import logging as _logging
    _logger = _logging.getLogger(__name__)

    capm = tasa_rf + beta * (market_return - tasa_rf)
    wacc = calcular_wacc(beta, debt, equity, cost_of_debt, tax_rate, tasa_rf)

    # --- Validaciones WACC ---
    wacc_below_rf: Optional[bool] = None
    wacc_below_rf_aviso: Optional[str] = None
    wacc_spread_bajo_aviso: Optional[str] = None

    if wacc is not None:
        if wacc <= tasa_rf:
            wacc_below_rf = True
            wacc_below_rf_aviso = (
                f"WACC calculado ({wacc:.2%}) es menor o igual que la tasa libre de riesgo "
                f"({tasa_rf:.2%}). El valor terminal puede estar fuertemente inflado. "
                f"Revisar el costo de deuda (Kd) y la estructura de capital."
            )
            _logger.warning(wacc_below_rf_aviso)
        else:
            wacc_below_rf = False

        g_terminal = G_TERMINAL
        if tasa_crecimiento is not None:
            spread = wacc - g_terminal
            if spread < 0.005:
                wacc_spread_bajo_aviso = (
                    f"Spread WACC ({wacc:.2%}) − g terminal ({g_terminal:.2%}) = "
                    f"{spread:.2%} extremadamente bajo. El valor terminal puede estar inflado."
                )

    revenue_per_share_raw = info.get("revenuePerShare")
    revenue_per_share = to_float(revenue_per_share_raw) if revenue_per_share_raw else None
    book_value_raw = info.get("bookValue")
    book_value = to_float(book_value_raw) if book_value_raw else None

    ps_ratio = (precio / revenue_per_share) if revenue_per_share else None
    pb_ratio = (precio / book_value) if book_value else None
    roe = info.get("returnOnEquity")
    debt_to_capital = (debt / (debt + equity)) if (debt + equity) else 0
    volume = to_float(info.get("volume"), 0)
    revenue_growth = info.get("revenueGrowth")
    filtros = [
        {
            "nombre": "P/E",
            "descripcion": "Price to Earnings — Precio por cada peso de ganancia neta",
            "valor": f"{pe_ratio:.2f}" if pe_ratio is not None else "N/D",
            "criterio": "< 20",
            "cumple": pe_ratio is not None and pe_ratio <= 20,
        },
        {
            "nombre": "P/S",
            "descripcion": "Price to Sales — Precio por cada peso de ventas",
            "valor": f"{ps_ratio:.2f}" if ps_ratio is not None else "N/D",
            "criterio": "< 2",
            "cumple": ps_ratio is not None and ps_ratio <= 2,
        },
        {
            "nombre": "P/B",
            "descripcion": "Price to Book — Precio sobre el valor contable de la empresa",
            "valor": f"{pb_ratio:.2f}" if pb_ratio is not None else "N/D",
            "criterio": "< 1",
            "cumple": pb_ratio is not None and pb_ratio <= 1,
        },
        {
            "nombre": "ROE",
            "descripcion": "Return on Equity — Rentabilidad sobre el patrimonio neto",
            "valor": f"{roe:.2%}" if isinstance(roe, (int, float)) else "N/D",
            "criterio": "> 10%",
            "cumple": isinstance(roe, (int, float)) and roe > 0.10,
        },
        {
            "nombre": "Debt/Capital",
            "descripcion": "Deuda total sobre el capital total (deuda + equity)",
            "valor": f"{debt_to_capital:.2%}",
            "criterio": "< 25%",
            "cumple": debt_to_capital < 0.25,
        },
        {
            "nombre": "Volumen",
            "descripcion": "Volumen diario de acciones operadas en el mercado",
            "valor": f"{volume:,.0f}" if volume else "N/D",
            "criterio": "> 250k",
            "cumple": bool(volume and volume > 250000),
        },
        {
            "nombre": "Revenue Growth",
            "descripcion": "Crecimiento anual de los ingresos de la empresa",
            "valor": f"{revenue_growth:.2%}" if isinstance(revenue_growth, (int, float)) else "N/D",
            "criterio": "> 0%",
            "cumple": isinstance(revenue_growth, (int, float)) and revenue_growth > 0,
        },
    ]

    año_actual = datetime.now().year
    fcf_historico = [
        {"anio": (year if year is not None else año_actual - i), "valor": to_billions(valor)}
        for i, (year, valor) in enumerate(fcf_presentacion[:7])
    ]

    crecimiento_largo_plazo = G_TERMINAL  # unificado con Reverse DCF (2.5%)

    # Corrección 3: FCF negativo/cero → DCF no aplicable (evita heurística de convergencia)
    # Corrección 1: WACC None → DCF no aplicable
    if not fcf or fcf_actual <= 0 or wacc is None:
        fcf_proyectado: list[float] = []
        fcf_proyecciones: list[dict] = []
        valor_total = None
        equity_value = None
        valor_por_accion = None
        diferencia = None
        diferencia_pct = None
    else:
        fcf_proyectado = proyectar_fcf(fcf_actual, tasa_crecimiento)
        fcf_proyecciones = [
            {"anio": año_actual + i, "valor": to_billions(valor)}
            for i, valor in enumerate(fcf_proyectado, start=1)
        ]
        valor_total = calcular_valor_intrinseco(fcf_proyectado, wacc)
        # Corrección 4: restar deuda neta total (LP + corriente − caja) en vez de solo LP
        equity_value = (valor_total - net_debt_total) if valor_total is not None else None
        valor_por_accion = (equity_value / acciones) if equity_value is not None and acciones else None
        diferencia = (valor_por_accion - precio) if valor_por_accion is not None else None
        diferencia_pct = ((diferencia / precio) * 100) if diferencia is not None and precio else None

    dividend_yield = normalizar_dividend_yield(
        info.get("dividendYield"), info.get("dividendRate"), precio
    )
    total_assets = info.get("totalAssets")
    total_liabilities = info.get("totalLiab")
    net_worth_per_share = None
    if total_assets and total_liabilities and acciones:
        net_worth_per_share = (total_assets - total_liabilities) / acciones

    safety_margin = None
    if valor_por_accion is not None and precio:
        try:
            safety_margin = (valor_por_accion - precio) / precio
        except ZeroDivisionError:
            safety_margin = None

    filtros.append({
        "nombre": "Safety Margin",
        "descripcion": "Margen de seguridad — Diferencia entre valor intrínseco y precio de mercado",
        "valor": f"{safety_margin:.2%}" if isinstance(safety_margin, (int, float)) else "N/D",
        "criterio": "> 0%",
        "cumple": isinstance(safety_margin, (int, float)) and safety_margin > 0,
    })

    valor_terminal = None
    if valor_total is not None and wacc is not None:
        fcf_final = fcf_proyectado[-1] if fcf_proyectado else 0
        if wacc > 0 and crecimiento_largo_plazo < wacc:
            valor_terminal = (fcf_final * (1 + crecimiento_largo_plazo)) / (wacc - crecimiento_largo_plazo)

    # Chequeo de escala: yfinance a veces devuelve revenue/GP/EBITDA en moneda local
    # sin conversión (p.ej. YPF en ARS), produciendo valores 1000x demasiado altos.
    # Umbral de 100x para no afectar empresas con P/S bajo pero legítimo.
    revenue_raw = to_optional_float(info.get("totalRevenue"))
    gross_profit_raw = to_optional_float(info.get("grossProfits"))
    escala_ajustada = False
    escala_aviso = None
    if revenue_raw is not None and equity > 0 and revenue_raw > equity * 100:
        _rev_b_antes = revenue_raw / 1e9
        _mcap_b = equity / 1e9
        revenue_raw = revenue_raw / 1000
        if gross_profit_raw is not None:
            gross_profit_raw = gross_profit_raw / 1000
        if ebitda_val is not None:
            ebitda_val = ebitda_val / 1000
        escala_ajustada = True
        escala_aviso = (
            f"Escala ajustada (÷1000): revenue reportado ${_rev_b_antes:,.0f}B vs "
            f"market cap ${_mcap_b:.1f}B — posible conversión de moneda local sin ajustar."
        )

    annual_dividend = to_optional_float(info.get("dividendRate"))
    detalles_metricas = metricas_fuente or {}
    eps_ttm = to_optional_float(info.get("trailingEps"))
    net_income_ttm = to_optional_float(info.get("netIncomeToCommon"))
    if net_income_ttm is None and income_stmt is not None and not income_stmt.empty and "Net Income" in income_stmt.index:
        net_income_series = income_stmt.loc["Net Income"].dropna()
        if not net_income_series.empty:
            net_income_ttm = to_optional_float(net_income_series.iloc[0])

    payout_ratio = to_optional_float(info.get("payoutRatio"))
    if payout_ratio is None and eps_ttm is not None and eps_ttm > 0 and annual_dividend is not None:
        payout_ratio = annual_dividend / eps_ttm

    # Revenue history for coefficient of variation (used by company_stage.py to detect irregular revenue)
    # Si el income_stmt tiene un año fiscal parcial como primer columna, omitirlo.
    revenue_historico_raw: list[float] = []
    try:
        if income_stmt is not None and not income_stmt.empty:
            for _rev_label in ("Total Revenue", "Revenue"):
                if _rev_label in income_stmt.index:
                    _rev_s = income_stmt.loc[_rev_label].dropna()
                    if _income_stmt_parcial and len(_rev_s) > 1:
                        _rev_s = _rev_s.iloc[1:]  # omitir año parcial en curso
                    _rev_s = _rev_s.head(5)
                    _rev_list = [to_optional_float(v) for v in _rev_s.tolist()]
                    revenue_historico_raw = [v for v in _rev_list if v is not None]
                    break
    except Exception:
        revenue_historico_raw = []

    # Revenue and net income with year labels for charts
    revenue_historico_labeled: list[dict] = []
    try:
        if income_stmt is not None and not income_stmt.empty:
            for _rev_label in ("Total Revenue", "Revenue"):
                if _rev_label in income_stmt.index:
                    _rev_s = income_stmt.loc[_rev_label].dropna()
                    if _income_stmt_parcial and len(_rev_s) > 1:
                        _rev_s = _rev_s.iloc[1:]
                    _rev_s = _rev_s.head(5)
                    for _col, _val in _rev_s.items():
                        _v = to_optional_float(_val)
                        if _v is not None:
                            try:
                                _yr = _col.year if hasattr(_col, 'year') else int(str(_col)[:4])
                            except Exception:
                                _yr = None
                            revenue_historico_labeled.append({"anio": _yr, "valor": to_billions(_v)})
                    break
    except Exception:
        revenue_historico_labeled = []

    net_income_historico_labeled: list[dict] = []
    try:
        if income_stmt is not None and not income_stmt.empty and "Net Income" in income_stmt.index:
            _ni_s = income_stmt.loc["Net Income"].dropna()
            if _income_stmt_parcial and len(_ni_s) > 1:
                _ni_s = _ni_s.iloc[1:]
            _ni_s = _ni_s.head(5)
            for _col, _val in _ni_s.items():
                _v = to_optional_float(_val)
                if _v is not None:
                    try:
                        _yr = _col.year if hasattr(_col, 'year') else int(str(_col)[:4])
                    except Exception:
                        _yr = None
                    net_income_historico_labeled.append({"anio": _yr, "valor": to_billions(_v)})
    except Exception:
        net_income_historico_labeled = []

    # ── Derived metrics for 6-category data accordion ─────────────────────
    # FCF TTM raw: usar el valor trimestral (más preciso) si está disponible,
    # sino el primero de la serie anual filtrada.
    _fcf_ttm_raw = _fcf_ttm_q if _fcf_ttm_q is not None else (fcf[0] if fcf else None)
    _ta_final = total_assets_val if total_assets_val is not None else to_optional_float(info.get("totalAssets"))
    _tl_final = total_liab_val if total_liab_val is not None else to_optional_float(info.get("totalLiab"))

    roa_raw = to_optional_float(info.get("returnOnAssets"))
    current_ratio_raw = to_optional_float(info.get("currentRatio"))
    fifty_two_week_high = to_optional_float(info.get("fiftyTwoWeekHigh"))

    p_fcf_raw = (equity / _fcf_ttm_raw) if (_fcf_ttm_raw and _fcf_ttm_raw > 0 and equity) else None
    fcf_per_share = (_fcf_ttm_raw / acciones) if (_fcf_ttm_raw is not None and acciones) else None
    fcf_yield_pct = (_fcf_ttm_raw / equity * 100) if (_fcf_ttm_raw is not None and equity) else None

    _ew = equity + debt
    equity_weight_pct = (equity / _ew * 100) if _ew else None
    debt_weight_pct = (debt / _ew * 100) if _ew else None
    kd_after_tax_pct = (cost_of_debt * (1 - tax_rate) * 100) if (cost_of_debt is not None and tax_rate is not None) else None

    _cap_total = equity + total_debt
    capital_bar_equity_pct = (equity / _cap_total * 100) if _cap_total else None
    capital_bar_debt_pct = (total_debt / _cap_total * 100) if _cap_total else None

    patrimonio_neto = (_ta_final - _tl_final) if (_ta_final is not None and _tl_final is not None) else None

    gross_margin_pct = (gross_profit_raw / revenue_raw * 100) if (gross_profit_raw is not None and revenue_raw) else None
    ebitda_margin_pct = (ebitda_val / revenue_raw * 100) if (ebitda_val is not None and revenue_raw) else None
    net_margin_pct_ttm = (net_income_ttm / revenue_raw * 100) if (net_income_ttm is not None and revenue_raw) else None

    roe_val = to_optional_float(roe)
    roe_pct = (roe_val * 100) if roe_val is not None else None
    roa_pct = (roa_raw * 100) if roa_raw is not None else None
    debt_to_capital_pct = debt_to_capital * 100

    gross_margin_historico_labeled: list[dict] = []
    ebitda_margin_historico_labeled_pct: list[dict] = []
    net_margin_historico_labeled_pct: list[dict] = []
    try:
        if income_stmt is not None and not income_stmt.empty:
            def _hist_series(row_label, n=5):
                """Extrae serie histórica limpia, omitiendo año parcial si aplica."""
                if row_label not in income_stmt.index:
                    return None
                s = income_stmt.loc[row_label].dropna()
                if _income_stmt_parcial and len(s) > 1:
                    s = s.iloc[1:]
                return s.head(n)

            _rev_row2 = None
            for _rev_lbl2 in ("Total Revenue", "Revenue"):
                if _rev_lbl2 in income_stmt.index:
                    _rev_row2 = _hist_series(_rev_lbl2)
                    break
            _gp_row = _hist_series("Gross Profit")
            _ni_row2 = _hist_series("Net Income")
            _ebitda_row = None
            for _eb_lbl in ("EBITDA", "Reconciled EBITDA", "Normalized EBITDA"):
                if _eb_lbl in income_stmt.index:
                    _ebitda_row = _hist_series(_eb_lbl)
                    break
            if _rev_row2 is not None:
                for _col2, _rev_val2 in _rev_row2.items():
                    _rv2 = to_optional_float(_rev_val2)
                    if not _rv2:
                        continue
                    try:
                        _yr2 = _col2.year if hasattr(_col2, 'year') else int(str(_col2)[:4])
                    except Exception:
                        _yr2 = None
                    if _gp_row is not None and _col2 in _gp_row.index:
                        _gv2 = to_optional_float(_gp_row[_col2])
                        if _gv2 is not None:
                            gross_margin_historico_labeled.append({"anio": _yr2, "valor": round(_gv2 / _rv2 * 100, 1)})
                    if _ebitda_row is not None and _col2 in _ebitda_row.index:
                        _ev2 = to_optional_float(_ebitda_row[_col2])
                        if _ev2 is not None:
                            ebitda_margin_historico_labeled_pct.append({"anio": _yr2, "valor": round(_ev2 / _rv2 * 100, 1)})
                    if _ni_row2 is not None and _col2 in _ni_row2.index:
                        _nv2 = to_optional_float(_ni_row2[_col2])
                        if _nv2 is not None:
                            net_margin_historico_labeled_pct.append({"anio": _yr2, "valor": round(_nv2 / _rv2 * 100, 1)})
    except Exception:
        gross_margin_historico_labeled = []
        ebitda_margin_historico_labeled_pct = []
        net_margin_historico_labeled_pct = []

    # Sort chronologically (oldest → newest) for table display
    gross_margin_historico_labeled.sort(key=lambda x: x.get("anio") or 0)
    ebitda_margin_historico_labeled_pct.sort(key=lambda x: x.get("anio") or 0)
    net_margin_historico_labeled_pct.sort(key=lambda x: x.get("anio") or 0)

    def _margin_trend(historico, ttm_val):
        if not historico or ttm_val is None:
            return None
        prev = historico[-1].get("valor")
        if prev is None:
            return None
        return "up" if ttm_val > prev else ("down" if ttm_val < prev else None)

    gross_margin_trend = _margin_trend(gross_margin_historico_labeled, gross_margin_pct)
    ebitda_margin_trend = _margin_trend(ebitda_margin_historico_labeled_pct, ebitda_margin_pct)
    net_margin_trend = _margin_trend(net_margin_historico_labeled_pct, net_margin_pct_ttm)

    datos_empresa = {
        "nombre": nombre,
        "sector": sector,
        "industria": info.get("industry"),
        "descripcion": info.get("longBusinessSummary"),
        "pais": info.get("country"),
        "ciudad": info.get("city"),
        "sitio_web": info.get("website"),
        "empleados": info.get("fullTimeEmployees"),
        # Datos adicionales para valuación multi-modelo
        "eps_ttm": eps_ttm,
        "eps_forward": to_optional_float(info.get("forwardEps")),
        "revenue_ttm": revenue_raw,
        "revenue_ttm_billones": to_billions(revenue_raw),
        "revenue_ttm_display": smart_format_billions(revenue_raw),
        "gross_profit_ttm": gross_profit_raw,
        "gross_profit_ttm_billones": to_billions(gross_profit_raw),
        "gross_profit_ttm_display": smart_format_billions(gross_profit_raw),
        "escala_ajustada": escala_ajustada,
        "escala_aviso": escala_aviso,
        "net_income_ttm": net_income_ttm,
        "net_income_ttm_billones": to_billions(net_income_ttm),
        "net_income_ttm_display": smart_format_billions(net_income_ttm),
        "pe_ratio_raw": pe_ratio,
        "ps_ratio_raw": ps_ratio,
        "pb_ratio_raw": pb_ratio,
        "roe_raw": to_optional_float(roe),
        "debt_to_capital": debt_to_capital,
        "fcf_ttm": fcf[0] if fcf else None,
        "fcf_ttm_billones": to_billions(fcf[0]) if fcf else None,
        "fcf_ttm_display": smart_format_billions(fcf[0]) if fcf else None,
        "precio_actual": precio,
        "acciones": acciones,
        "acciones_billones": to_billions(acciones),
        "acciones_ajuste_aviso": acciones_ajuste_aviso,
        "market_cap": equity,
        "market_cap_billones": to_billions(equity),
        "deuda": debt,
        "deuda_billones": to_billions(debt),
        "deuda_corriente": current_debt,
        "deuda_corriente_billones": to_billions(current_debt),
        "deuda_total": total_debt,
        "deuda_total_billones": to_billions(total_debt),
        "caja": cash,
        "caja_billones": to_billions(cash),
        "deuda_neta": net_debt_total,          # LP + corriente − caja
        "deuda_neta_billones": to_billions(net_debt_total),
        # Balance sheet extras for Liquidation Value and Altman Z-Score
        "total_current_assets": total_current_assets_val,
        "total_current_assets_billones": to_billions(total_current_assets_val),
        "total_current_liabilities": total_current_liabilities_val,
        "total_assets": total_assets_val if total_assets_val is not None else to_optional_float(info.get("totalAssets")),
        "total_liabilities": total_liab_val if total_liab_val is not None else to_optional_float(info.get("totalLiab")),
        "total_liabilities_billones": to_billions(total_liab_val if total_liab_val is not None else to_optional_float(info.get("totalLiab"))),
        "retained_earnings": retained_earnings_val,
        "ebit": ebit_val,
        "ebitda_ttm": ebitda_val,
        "ebitda_ttm_billones": to_billions(ebitda_val),
        "ebitda_ttm_display": smart_format_billions(ebitda_val),
        "working_capital": working_capital_val,
        "eps_growth_5y": eps_growth_5y,
        "eps_growth_5y_fuente": eps_growth_5y_fuente,
        "revenue_historico": revenue_historico_raw,
        "revenue_historico_labeled": revenue_historico_labeled,
        "net_income_historico_labeled": net_income_historico_labeled,
        "beta": beta,
        "beta_aviso": beta_aviso,
        "tasa_impositiva": tax_rate,
        "tasa_impositiva_pct": tax_rate * 100 if tax_rate is not None else None,
        "cost_of_debt": cost_of_debt,
        "cost_of_debt_pct": cost_of_debt * 100 if cost_of_debt is not None else None,
        "metodo_crecimiento": metodo_utilizado,
        "metodo_crecimiento_codigo": metodo_codigo,
        "metodo_crecimiento_detalle": "Selección automática: se usa la tasa más cercana a cero.",
        "tasa_impositiva_fuente": detalles_metricas.get("tax_rate", {}).get("descripcion"),
        "tasa_impositiva_anios": detalles_metricas.get("tax_rate", {}).get("años"),
        "cost_of_debt_fuente": detalles_metricas.get("cost_of_debt", {}).get("descripcion"),
        "cost_of_debt_anios": detalles_metricas.get("cost_of_debt", {}).get("años"),
        "payout_ratio": payout_ratio,
        "payout_ratio_pct": payout_ratio * 100 if payout_ratio is not None else None,
        "roa_raw": roa_raw,
        "roa_pct": roa_pct,
        "current_ratio_raw": current_ratio_raw,
        "fifty_two_week_high": fifty_two_week_high,
        "p_fcf_raw": p_fcf_raw,
        "fcf_per_share": fcf_per_share,
        "fcf_yield_pct": fcf_yield_pct,
        "roe_pct": roe_pct,
        "debt_to_capital_pct": debt_to_capital_pct,
        "equity_weight_pct": equity_weight_pct,
        "debt_weight_pct": debt_weight_pct,
        "kd_after_tax_pct": kd_after_tax_pct,
        "g_terminal_pct": G_TERMINAL * 100,
        "capital_bar_equity_pct": capital_bar_equity_pct,
        "capital_bar_debt_pct": capital_bar_debt_pct,
        "patrimonio_neto_billones": to_billions(patrimonio_neto),
        "gross_margin_pct": gross_margin_pct,
        "ebitda_margin_pct": ebitda_margin_pct,
        "net_margin_pct": net_margin_pct_ttm,
        "gross_margin_historico_labeled": gross_margin_historico_labeled,
        "ebitda_margin_historico_labeled_pct": ebitda_margin_historico_labeled_pct,
        "net_margin_historico_labeled_pct": net_margin_historico_labeled_pct,
        "gross_margin_trend": gross_margin_trend,
        "ebitda_margin_trend": ebitda_margin_trend,
        "net_margin_trend": net_margin_trend,
    }

    metricas = {
        "tasa_rf": tasa_rf,
        "tasa_rf_pct": tasa_rf * 100 if tasa_rf is not None else None,
        "rf_fuente": rf_fuente,
        "market_return": market_return,
        "market_return_pct": market_return * 100 if market_return is not None else None,
        "capm": capm,
        "capm_pct": capm * 100 if capm is not None else None,
        "wacc": wacc,
        "wacc_pct": wacc * 100 if wacc is not None else None,
        "crecimiento": tasa_crecimiento,
        "crecimiento_pct": tasa_crecimiento * 100 if tasa_crecimiento is not None else None,
        "crecimiento_cagr": crecimiento,
        "crecimiento_cagr_pct": crecimiento * 100 if crecimiento is not None else None,
        "cagr_fcf_todos_negativos": crecimiento is None and avg_growth_rate is None,
        "cagr_cap_applied": cagr_cap_applied,
        "cagr_cap_aviso": (
            f"CAGR histórico {cagr_antes_cap:.1%} — capeado al {_CAGR_CAP_DCF:.0%} por "
            "año base anómalo. Considerar usar el Reverse DCF para evaluar qué "
            "crecimiento implica el precio actual."
            if cagr_cap_applied else None
        ),
        "crecimiento_promedio": avg_growth_rate,
        "crecimiento_promedio_pct": avg_growth_rate * 100 if avg_growth_rate is not None else None,
        "valor_terminal": to_billions(valor_terminal),
        "detalles_fuente": detalles_metricas,
        "wacc_below_rf": wacc_below_rf,
        "wacc_below_rf_aviso": wacc_below_rf_aviso,
        "wacc_spread_bajo_aviso": wacc_spread_bajo_aviso,
    }

    # Dividend CAGR histórico (para el modelo DDM)
    _dividend_cagr: Optional[float] = None
    _dividend_years: int = 0
    try:
        _divs = getattr(empresa_yf, "dividends", None)
        if _divs is not None and not _divs.empty:
            _annual: dict[int, float] = {}
            for _dt, _val in zip(_divs.index, _divs.values):
                try:
                    _yr = pd.Timestamp(_dt).year
                except Exception:
                    continue
                _annual[_yr] = _annual.get(_yr, 0.0) + float(_val)
            _annual_vals = [v for _, v in sorted(_annual.items()) if v > 0]
            if len(_annual_vals) >= 2:
                _n = len(_annual_vals) - 1
                if _annual_vals[0] > 0 and _annual_vals[-1] > 0:
                    _dividend_cagr = (_annual_vals[-1] / _annual_vals[0]) ** (1 / _n) - 1
                    _dividend_years = len(_annual_vals)
    except Exception:
        pass

    dividendos = {
        "yield": dividend_yield,
        "yield_pct": dividend_yield * 100 if dividend_yield is not None else None,
        "annual_dividend": annual_dividend,
        "paga": annual_dividend is not None and annual_dividend > 0,
        "net_worth_per_share": net_worth_per_share,
        "safety_margin": safety_margin,
        "safety_margin_pct": safety_margin * 100 if safety_margin is not None else None,
        "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
        "dividend_cagr": _dividend_cagr,
        "dividend_cagr_pct": round(_dividend_cagr * 100, 2) if _dividend_cagr is not None else None,
        "dividend_years": _dividend_years,
    }

    net_margin = to_optional_float(info.get("profitMargins"))

    analisis_tecnico = calcular_analisis_tecnico(empresa_yf, precio)

    if skip_news:
        noticias: List[dict] = []
        noticias_fuentes: set = set()
        noticias_error = None
        resumen_noticias = None
        resumen_noticias_error = None
    else:
        noticias, noticias_fuentes, noticias_error = _fetch_news(ticker, empresa_yf, nombre)
        resumen_noticias, resumen_noticias_error = _generate_ai_summary(noticias, ticker, nombre)

    mapa_fuentes = {
        "marketaux": "Marketaux",
        "finnhub": "Finnhub",
        "yfinance": "YFinance",
    }
    fuentes_detectadas = [mapa_fuentes.get(f, f.title()) for f in sorted(noticias_fuentes)]
    noticias_fuente_descripcion = ", ".join(fuentes_detectadas) if fuentes_detectadas else None

    estado = None
    if valor_por_accion is not None and precio:
        if valor_por_accion > precio * 1.1:
            estado = "SUBVALUADA"
        elif valor_por_accion < precio * 0.9:
            estado = "SOBREVALUADA"
        else:
            estado = "RAZONABLE"

    return {
        "nombre": nombre,
        "sector": sector,
        "valor_intrinseco": valor_por_accion,
        "precio_actual": precio,
        "diferencia": diferencia,
        "diferencia_pct": diferencia_pct,
        "estado": estado,
        "datos_empresa": datos_empresa,
        "filtros": filtros,
        "metricas": metricas,
        "fcf_historico": fcf_historico,
        "fcf_proyectado": fcf_proyecciones,
        "dividendos": dividendos,
        "metricas_fuente": detalles_metricas,
        "noticias": noticias,
        "noticias_fuente": ",".join(sorted(noticias_fuentes)) if noticias_fuentes else None,
        "noticias_error": noticias_error,
        "noticias_fuente_descripcion": noticias_fuente_descripcion,
        "resumen_noticias": resumen_noticias,
        "resumen_noticias_error": resumen_noticias_error,
        "analisis_tecnico": analisis_tecnico,
        # Señales para detección de etapa empresarial
        "net_margin": net_margin,
        "revenue_growth_raw": to_optional_float(revenue_growth),
        "has_dividends": (dividend_yield is not None and dividend_yield > 0.005),
        # Advertencia de datos parciales (año fiscal en curso excluido de series históricas)
        "aviso_datos_parciales": (
            "El período fiscal más reciente en los datos anuales de yfinance representa "
            "un año incompleto y fue excluido de las series históricas (CAGR, revenue histórico, "
            "FCF histórico). Los valores TTM se calculan desde datos trimestrales."
        ) if (_income_stmt_parcial or _cashflow_parcial) else None,
    }


def build_filtros_por_etapa(resultado: dict, stage: int) -> list:
    """Genera filtros financieros con umbrales adaptados a la etapa del ciclo de vida.

    Retorna una lista con el mismo formato que resultado['filtros'].
    Si stage no está entre 1-6, devuelve los filtros originales como fallback.
    """
    d = resultado.get("datos_empresa") or {}
    precio = resultado.get("precio_actual") or 0
    net_margin = resultado.get("net_margin")

    pe = d.get("pe_ratio_raw")
    ps = d.get("ps_ratio_raw")
    market_cap = d.get("market_cap") or 0
    fcf_ttm = d.get("fcf_ttm")
    revenue_ttm = d.get("revenue_ttm")
    gross_profit_ttm = d.get("gross_profit_ttm")
    deuda_total = d.get("deuda_total") or 0
    total_assets = d.get("total_assets")
    total_liabilities = d.get("total_liabilities")
    eps_forward = d.get("eps_forward")

    equity_book = (
        (total_assets - total_liabilities)
        if total_assets is not None and total_liabilities is not None
        else None
    )

    def _fmt(v, pct=False):
        if v is None:
            return "N/D"
        return f"{v:.1%}" if pct else f"{v:.2f}x"

    p_fcf = (market_cap / fcf_ttm) if (fcf_ttm and fcf_ttm > 0 and market_cap) else None
    p_gp = (market_cap / gross_profit_ttm) if (gross_profit_ttm and gross_profit_ttm > 0 and market_cap) else None
    fwd_pe = (precio / eps_forward) if (eps_forward and eps_forward > 0 and precio) else None
    roe = (
        (revenue_ttm * net_margin) / equity_book
        if (revenue_ttm and net_margin and equity_book and equity_book > 0)
        else None
    )
    debt_to_cap = (
        deuda_total / (deuda_total + equity_book)
        if (equity_book and (deuda_total + equity_book) > 0)
        else None
    )

    if stage in (1, 2):
        return [
            {
                "nombre": "P/S",
                "descripcion": "Price to Sales — relevante para empresas de alto crecimiento sin beneficios",
                "valor": _fmt(ps),
                "criterio": "< 15",
                "cumple": ps is not None and ps <= 15,
            },
            {
                "nombre": "P/Gross Profit",
                "descripcion": "Price to Gross Profit — proxy de eficiencia operativa en etapas tempranas",
                "valor": _fmt(p_gp),
                "criterio": "< 20",
                "cumple": p_gp is not None and p_gp <= 20,
            },
        ]

    if stage == 3:
        return [
            {
                "nombre": "P/S",
                "descripcion": "Price to Sales",
                "valor": _fmt(ps),
                "criterio": "< 10",
                "cumple": ps is not None and ps <= 10,
            },
            {
                "nombre": "Forward P/E",
                "descripcion": "Price to Forward Earnings — empresa en punto de quiebre, usar estimación futura",
                "valor": _fmt(fwd_pe),
                "criterio": "< 40",
                "cumple": fwd_pe is not None and fwd_pe <= 40,
            },
        ]

    if stage == 4:
        return [
            {
                "nombre": "P/E",
                "descripcion": "Price to Earnings trailing",
                "valor": _fmt(pe),
                "criterio": "< 35",
                "cumple": pe is not None and pe <= 35,
            },
            {
                "nombre": "P/S",
                "descripcion": "Price to Sales",
                "valor": _fmt(ps),
                "criterio": "< 8",
                "cumple": ps is not None and ps <= 8,
            },
            {
                "nombre": "P/FCF",
                "descripcion": "Price to Free Cash Flow trailing",
                "valor": _fmt(p_fcf),
                "criterio": "< 35",
                "cumple": p_fcf is not None and p_fcf <= 35,
            },
        ]

    if stage == 5:
        return [
            {
                "nombre": "P/E",
                "descripcion": "Price to Earnings trailing",
                "valor": _fmt(pe),
                "criterio": "< 25",
                "cumple": pe is not None and pe <= 25,
            },
            {
                "nombre": "P/FCF",
                "descripcion": "Price to Free Cash Flow trailing",
                "valor": _fmt(p_fcf),
                "criterio": "< 25",
                "cumple": p_fcf is not None and p_fcf <= 25,
            },
            {
                "nombre": "P/S",
                "descripcion": "Price to Sales",
                "valor": _fmt(ps),
                "criterio": "< 5",
                "cumple": ps is not None and ps <= 5,
            },
            {
                "nombre": "ROE",
                "descripcion": "Return on Equity",
                "valor": _fmt(roe, pct=True),
                "criterio": "> 10%",
                "cumple": roe is not None and roe > 0.10,
            },
        ]

    if stage == 6:
        return [
            {
                "nombre": "Debt/Capital",
                "descripcion": "Deuda total sobre capital total — crítico en etapa de declive",
                "valor": _fmt(debt_to_cap, pct=True),
                "criterio": "< 40%",
                "cumple": debt_to_cap is not None and debt_to_cap < 0.40,
            },
        ]

    return resultado.get("filtros") or []
