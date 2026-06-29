"""
Detección automática de fase del ciclo económico.

Combina indicadores macro de la API de FRED con rotación sectorial
calculada con yfinance para determinar en qué fase del ciclo se encuentra
la economía: Early Expansion, Late Expansion, Early Contraction, Late Contraction.

Mejoras v2:
- Scoring con tendencias: nivel 70% + delta 30% por indicador macro
- Detalle numérico del scoring en campo scoring_detalle
- Warning de concentración sectorial en XLK
"""

import math
import os
import time
from typing import Optional

import requests
import yfinance as yf

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# Sectores ETF y su fase de ciclo favorable
_SECTOR_ETFS = {
    "XLK":  {"nombre": "Tecnología",       "fase": "early_expansion"},
    "XLY":  {"nombre": "Consumo discr.",    "fase": "early_expansion"},
    "XLI":  {"nombre": "Industria",         "fase": "late_expansion"},
    "XLB":  {"nombre": "Materiales",        "fase": "late_expansion"},
    "XLE":  {"nombre": "Energía",           "fase": "early_contraction"},
    "XLP":  {"nombre": "Consumo básico",    "fase": "early_contraction"},
    "XLU":  {"nombre": "Utilities",         "fase": "late_contraction"},
    "XLF":  {"nombre": "Finanzas",          "fase": "late_contraction"},
    "IYZ":  {"nombre": "Telecom",           "fase": "late_contraction"},
    "XLV":  {"nombre": "Salud",             "fase": "late_expansion"},
}

_PHASE_LABELS = {
    "early_expansion":   "Expansión Temprana",
    "late_expansion":    "Expansión Tardía",
    "early_contraction": "Contracción Temprana",
    "late_contraction":  "Contracción Tardía",
}

_PHASE_ORDER = ["early_expansion", "late_expansion", "early_contraction", "late_contraction"]

# Sectores favorables por fase
_FAVORABLE_SECTORS = {
    "early_expansion":   ["XLK", "XLY", "XLI"],
    "late_expansion":    ["XLI", "XLB", "XLV", "XLE"],
    "early_contraction": ["XLE", "XLP", "XLV"],
    "late_contraction":  ["XLU", "XLF", "IYZ", "XLP"],
}

# Posición en el eje sinusoidal (0..1) de cada fase
_PHASE_POSITION = {
    "early_expansion":   0.15,
    "late_expansion":    0.40,
    "early_contraction": 0.65,
    "late_contraction":  0.88,
}

# Colores por fase
_PHASE_COLORS = {
    "early_expansion":   "#22c55e",
    "late_expansion":    "#84cc16",
    "early_contraction": "#f97316",
    "late_contraction":  "#ef4444",
}


# ---------------------------------------------------------------------------
# Obtención de datos FRED
# ---------------------------------------------------------------------------

def _fred_history(series_id: str, api_key: str, limit: int = 80) -> list[float]:
    """
    Devuelve hasta `limit` valores no nulos de una serie FRED, ordenados de
    más reciente a más antiguo. Permite calcular tanto el nivel actual como
    deltas históricos con una sola petición.
    """
    try:
        resp = requests.get(
            _FRED_BASE,
            params={
                "series_id":  series_id,
                "api_key":    api_key,
                "file_type":  "json",
                "sort_order": "desc",
                "limit":      limit,
            },
            timeout=8,
        )
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        values: list[float] = []
        for o in obs:
            v = o.get("value", ".")
            if v != ".":
                values.append(float(v))
        return values
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Scores de nivel (sin cambios vs v1)
# ---------------------------------------------------------------------------

def _score_yield_curve(spread: Optional[float]) -> float:
    """T10Y2Y: curva invertida = recesión inminente."""
    if spread is None:
        return 0.0
    if spread > 1.0:
        return 2.0
    if spread > 0.0:
        return 1.0
    if spread > -0.5:
        return -1.0
    return -2.0


def _score_cfnai(cfnai: Optional[float]) -> float:
    """CFNAI: positivo = actividad por encima de la tendencia histórica."""
    if cfnai is None:
        return 0.0
    if cfnai >= 0.5:
        return 2.0
    if cfnai >= 0.0:
        return 1.0
    if cfnai >= -0.5:
        return -1.0
    return -2.0


def _score_unemployment(unrate: Optional[float]) -> float:
    """UNRATE: desempleo bajo → expansión."""
    if unrate is None:
        return 0.0
    if unrate < 4.0:
        return 2.0
    if unrate < 5.0:
        return 1.0
    if unrate < 6.5:
        return -1.0
    return -2.0


def _score_cpi(cpi_yoy: Optional[float]) -> float:
    """Inflación YoY: moderada es normal; muy alta → late expansion / contraction."""
    if cpi_yoy is None:
        return 0.0
    if cpi_yoy < 2.5:
        return 1.5
    if cpi_yoy < 4.0:
        return 0.5
    if cpi_yoy < 6.0:
        return -0.5
    return -1.5


def _score_lei(lei: Optional[float]) -> float:
    """USSLIND (Philly Fed Leading Index): positivo = crecimiento sobre tendencia."""
    if lei is None:
        return 0.0
    if lei > 1.0:
        return 2.0
    if lei > 0.0:
        return 1.0
    if lei > -1.0:
        return -1.0
    return -2.0


# ---------------------------------------------------------------------------
# Scores de delta / tendencia (MEJORA 1)
# ---------------------------------------------------------------------------

def _delta_score_yield_curve(delta: Optional[float]) -> float:
    """
    Tendencia de la curva de rendimiento: normalización (delta > 0) es señal positiva.
    Ventana: 3 meses (serie diaria).
    """
    if delta is None:
        return 0.0
    if delta > 0.3:
        return 2.0
    if delta > 0.1:
        return 1.0
    if delta < -0.3:
        return -2.0
    if delta < -0.1:
        return -1.0
    return 0.0


def _delta_score_cfnai(delta: Optional[float]) -> float:
    """
    Tendencia del CFNAI: mejora en actividad económica (delta > 0) anticipa expansión.
    Ventana: 3 meses (serie mensual, índice 3).
    """
    if delta is None:
        return 0.0
    if delta > 0.3:
        return 2.0
    if delta > 0.1:
        return 1.0
    if delta < -0.3:
        return -2.0
    if delta < -0.1:
        return -1.0
    return 0.0


def _delta_score_unemployment(delta: Optional[float]) -> float:
    """
    Tendencia del desempleo (invertida): caída de tasa (delta < 0) es señal positiva.
    delta = unrate_actual - unrate_hace_6m. Ventana: 6 meses (índice 6).
    """
    if delta is None:
        return 0.0
    if delta < -0.3:
        return 2.0
    if delta < -0.1:
        return 1.0
    if delta > 0.3:
        return -2.0
    if delta > 0.1:
        return -1.0
    return 0.0


def _delta_score_inflation(delta: Optional[float]) -> float:
    """
    Tendencia de la inflación YoY: desaceleración (delta < 0) es señal positiva.
    delta = cpi_yoy_actual - cpi_yoy_hace_6m. Ventana: 6 meses.
    """
    if delta is None:
        return 0.0
    if delta < -0.5:
        return 2.0
    if delta < -0.2:
        return 1.0
    if delta > 0.5:
        return -2.0
    if delta > 0.2:
        return -1.0
    return 0.0


def _delta_score_lei(delta: Optional[float]) -> float:
    """
    Tendencia del índice líder (USSLIND): mejora (delta > 0) anticipa expansión.
    Ventana: 3 meses (serie mensual, índice 3).
    """
    if delta is None:
        return 0.0
    if delta > 0.5:
        return 2.0
    if delta > 0.2:
        return 1.0
    if delta < -0.5:
        return -2.0
    if delta < -0.2:
        return -1.0
    return 0.0


# ---------------------------------------------------------------------------
# Señales macro con nivel + delta
# ---------------------------------------------------------------------------

def _get_macro_signals(api_key: str) -> tuple[float, list[dict], dict]:
    """
    Obtiene indicadores macro de FRED y los puntúa combinando nivel (70%) y
    tendencia/delta (30%). Devuelve (score_total, lista_señales_ui, scoring_detalle).

    Si no hay suficientes datos históricos para calcular el delta de un indicador,
    se usa solo el nivel_score sin penalizar (delta_score = 0).
    """
    # ── Obtener historiales ─────────────────────────────────────────────────
    # T10Y2Y es diario; 3 meses ≈ 65 días hábiles → pedimos 80 para tener margen
    t10y2y_hist = _fred_history("T10Y2Y",   api_key, limit=80)
    time.sleep(0.25)
    # Series mensuales: CFNAI y LEI necesitan índice 3 (3m); UNRATE índice 6 (6m)
    cfnai_hist  = _fred_history("CFNAI",    api_key, limit=8)
    time.sleep(0.25)
    unrate_hist = _fred_history("UNRATE",   api_key, limit=12)
    time.sleep(0.25)
    lei_hist    = _fred_history("USSLIND",  api_key, limit=8)
    time.sleep(0.25)
    # CPI: obs[0..12] para YoY actual; obs[6..18] para YoY hace 6 meses → 22 obs
    cpi_hist    = _fred_history("CPIAUCSL", api_key, limit=22)

    # ── Valores actuales ────────────────────────────────────────────────────
    t10y2y  = t10y2y_hist[0]  if t10y2y_hist  else None
    cfnai   = cfnai_hist[0]   if cfnai_hist   else None
    unrate  = unrate_hist[0]  if unrate_hist  else None
    lei_val = lei_hist[0]     if lei_hist     else None

    cpi_yoy: Optional[float] = None
    if len(cpi_hist) >= 13:
        cpi_now = cpi_hist[0]
        cpi_12m = cpi_hist[12]
        if cpi_12m:
            cpi_yoy = (cpi_now - cpi_12m) / cpi_12m * 100

    # ── Deltas ─────────────────────────────────────────────────────────────
    # T10Y2Y: 3 meses atrás ≈ índice 64 en serie diaria
    t10y2y_past  = t10y2y_hist[64] if len(t10y2y_hist) > 64 else None
    t10y2y_delta = (
        (t10y2y - t10y2y_past)
        if t10y2y is not None and t10y2y_past is not None
        else None
    )

    # CFNAI: 3 meses atrás = índice 3 en serie mensual
    cfnai_past  = cfnai_hist[3] if len(cfnai_hist) > 3 else None
    cfnai_delta = (
        (cfnai - cfnai_past)
        if cfnai is not None and cfnai_past is not None
        else None
    )

    # UNRATE: 6 meses atrás = índice 6 en serie mensual
    unrate_past  = unrate_hist[6] if len(unrate_hist) > 6 else None
    unrate_delta = (
        (unrate - unrate_past)
        if unrate is not None and unrate_past is not None
        else None
    )

    # CPI YoY hace 6 meses: (obs[6] - obs[18]) / obs[18] * 100
    cpi_yoy_past: Optional[float] = None
    if len(cpi_hist) >= 19:
        c6, c18 = cpi_hist[6], cpi_hist[18]
        if c18:
            cpi_yoy_past = (c6 - c18) / c18 * 100
    cpi_delta = (
        (cpi_yoy - cpi_yoy_past)
        if cpi_yoy is not None and cpi_yoy_past is not None
        else None
    )

    # LEI: 3 meses atrás = índice 3 en serie mensual
    lei_past  = lei_hist[3] if len(lei_hist) > 3 else None
    lei_delta = (
        (lei_val - lei_past)
        if lei_val is not None and lei_past is not None
        else None
    )

    # ── Scores de nivel ─────────────────────────────────────────────────────
    t10y2y_nivel = _score_yield_curve(t10y2y)
    cfnai_nivel  = _score_cfnai(cfnai)
    unrate_nivel = _score_unemployment(unrate)
    cpi_nivel    = _score_cpi(cpi_yoy)
    lei_nivel    = _score_lei(lei_val)

    # ── Scores de delta ─────────────────────────────────────────────────────
    t10y2y_ds = _delta_score_yield_curve(t10y2y_delta)
    cfnai_ds  = _delta_score_cfnai(cfnai_delta)
    unrate_ds = _delta_score_unemployment(unrate_delta)
    cpi_ds    = _delta_score_inflation(cpi_delta)
    lei_ds    = _delta_score_lei(lei_delta)

    # ── Scores finales: nivel 70% + delta 30% ───────────────────────────────
    t10y2y_score = t10y2y_nivel * 0.70 + t10y2y_ds * 0.30
    cfnai_score  = cfnai_nivel  * 0.70 + cfnai_ds  * 0.30
    unrate_score = unrate_nivel * 0.70 + unrate_ds * 0.30
    cpi_score    = cpi_nivel    * 0.70 + cpi_ds    * 0.30
    lei_score    = lei_nivel    * 0.70 + lei_ds    * 0.30

    total = t10y2y_score + cfnai_score + unrate_score + cpi_score + lei_score

    # ── Lista de señales para la UI (compatible con versión anterior) ────────
    def _fmtv(v: Optional[float], unit: str = "") -> str:
        return f"{v:.2f}{unit}" if v is not None else "N/D"

    señales = [
        {"nombre": "Curva de rendimiento (T10Y2Y)", "score": t10y2y_score, "valor": _fmtv(t10y2y, "%")},
        {"nombre": "Actividad económica (CFNAI)",   "score": cfnai_score,  "valor": _fmtv(cfnai)},
        {"nombre": "Desempleo (UNRATE)",             "score": unrate_score, "valor": _fmtv(unrate, "%")},
        {"nombre": "Inflación CPI (YoY)",            "score": cpi_score,    "valor": _fmtv(cpi_yoy, "%")},
        {"nombre": "Índice líder (LEI)",             "score": lei_score,    "valor": _fmtv(lei_val)},
    ]

    # ── Detalle numérico del scoring (MEJORA 2) ──────────────────────────────
    def _r(v: Optional[float]) -> Optional[float]:
        return round(v, 4) if v is not None else None

    scoring_macro: dict = {
        "curva_rendimiento": {
            "valor":       _r(t10y2y),
            "delta":       _r(t10y2y_delta),
            "nivel_score": t10y2y_nivel,
            "delta_score": t10y2y_ds,
            "score_final": round(t10y2y_score, 4),
        },
        "cfnai": {
            "valor":       _r(cfnai),
            "delta":       _r(cfnai_delta),
            "nivel_score": cfnai_nivel,
            "delta_score": cfnai_ds,
            "score_final": round(cfnai_score, 4),
        },
        "desempleo": {
            "valor":       _r(unrate),
            "delta":       _r(unrate_delta),
            "nivel_score": unrate_nivel,
            "delta_score": unrate_ds,
            "score_final": round(unrate_score, 4),
        },
        "inflacion": {
            "valor":       _r(cpi_yoy),
            "delta":       _r(cpi_delta),
            "nivel_score": cpi_nivel,
            "delta_score": cpi_ds,
            "score_final": round(cpi_score, 4),
        },
        "lei": {
            "valor":       _r(lei_val),
            "delta":       _r(lei_delta),
            "nivel_score": lei_nivel,
            "delta_score": lei_ds,
            "score_final": round(lei_score, 4),
        },
        "macro_score_total":          round(total, 4),
        "macro_score_maximo_posible": 10,
    }

    return total, señales, scoring_macro


# ---------------------------------------------------------------------------
# Rotación sectorial
# ---------------------------------------------------------------------------

def _get_sector_rotation() -> tuple[float, list[dict], dict, Optional[dict]]:
    """
    Compara rendimiento a 90 días de ETFs sectoriales vs SPY.
    Devuelve (score_neto, lista_sectores, scoring_detalle, warning_concentracion).

    El warning de concentración se activa cuando XLK supera a SPY en más del 8%
    y simultáneamente más de 5 de los 9 ETFs restantes tienen retorno negativo
    vs SPY, lo que sugiere concentración en mega caps más que rotación broad-based.
    """
    tickers_needed = list(_SECTOR_ETFS.keys()) + ["SPY"]
    try:
        data = yf.download(
            tickers_needed,
            period="3mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        closes = data["Close"] if "Close" in data.columns else data
    except Exception:
        return 0.0, [], {}, None

    if closes.empty or "SPY" not in closes.columns:
        return 0.0, [], {}, None

    spy_start = closes["SPY"].dropna().iloc[0]
    spy_end   = closes["SPY"].dropna().iloc[-1]
    if spy_start == 0:
        return 0.0, [], {}, None
    spy_ret = (spy_end - spy_start) / spy_start

    fase_scores: dict[str, float] = {f: 0.0 for f in _PHASE_ORDER}
    resultados: list[dict] = []
    etf_detail: dict[str, dict] = {}

    xlk_relativo: Optional[float] = None
    other_negatives = 0

    for ticker, info in _SECTOR_ETFS.items():
        if ticker not in closes.columns:
            continue
        serie = closes[ticker].dropna()
        if len(serie) < 5:
            continue
        ret      = (serie.iloc[-1] - serie.iloc[0]) / serie.iloc[0]
        relativo = ret - spy_ret
        relativo_pct = round(relativo * 100, 2)

        # Contribución al score de la fase correspondiente
        if relativo > 0.03:
            contrib = 2.0
        elif relativo > 0.01:
            contrib = 1.0
        elif relativo < -0.03:
            contrib = -1.0
        else:
            contrib = 0.0

        fase_scores[info["fase"]] += contrib

        resultados.append({
            "ticker":      ticker,
            "nombre":      info["nombre"],
            "fase":        _PHASE_LABELS[info["fase"]],
            "retorno_rel": float(relativo_pct),
        })

        etf_detail[ticker] = {
            "retorno_vs_spy": float(relativo_pct),
            "score":          float(contrib),
            "fase":           info["fase"],
        }

        # Datos para el warning de concentración
        if ticker == "XLK":
            xlk_relativo = float(relativo)
        else:
            if relativo < 0:
                other_negatives += 1

    # Score neto: fases de expansión suman, de contracción restan
    expansion_score   = fase_scores["early_expansion"] + fase_scores["late_expansion"]
    contraction_score = fase_scores["early_contraction"] + fase_scores["late_contraction"]
    net = max(-4.0, min(4.0, expansion_score - contraction_score))
    sector_score = net / 2.0

    # ── Warning de concentración sectorial (MEJORA 3) ────────────────────────
    warning: Optional[dict] = None
    if xlk_relativo is not None and xlk_relativo > 0.08 and other_negatives > 5:
        warning = {
            "activo": True,
            "mensaje": (
                "El liderazgo del sector tecnológico puede "
                "reflejar concentración en mega caps (IA) "
                "más que una rotación broad-based. "
                "Interpretar con cautela."
            ),
            "xlk_vs_spy":    round(xlk_relativo * 100, 2),
            "etfs_negativos": other_negatives,
        }

    # ── Detalle de scoring sectorial (MEJORA 2) ──────────────────────────────
    scoring_sectorial: dict = {
        "por_etf":         etf_detail,
        "net_expansion":   round(expansion_score, 4),
        "net_contraction": round(contraction_score, 4),
        "sector_score":    round(sector_score, 4),
    }

    return sector_score, resultados, scoring_sectorial, warning


# ---------------------------------------------------------------------------
# Determinación de fase y posición (sin cambios vs v1)
# ---------------------------------------------------------------------------

def _determine_phase(macro_score: float, sector_score: float) -> tuple[str, float]:
    """
    Combina macro (60%) y sector (40%) para determinar la fase.
    Devuelve (phase_key, combined_score).
    """
    combined = macro_score * 0.6 + sector_score * 0.4

    if combined >= 3.5:
        return "early_expansion", combined
    if combined >= 0:
        return "late_expansion", combined
    if combined >= -3.5:
        return "early_contraction", combined
    return "late_contraction", combined


def _position_for_phase(phase: str, combined_score: float) -> float:
    """Devuelve position_pct (0..1) para el marcador en la curva sinusoidal."""
    base = _PHASE_POSITION[phase]
    span = 0.20
    if phase == "early_expansion":
        t = (combined_score - 3.5) / 4.0
    elif phase == "late_expansion":
        t = combined_score / 3.5
    elif phase == "early_contraction":
        t = (combined_score + 3.5) / 3.5
    else:
        t = (combined_score + 7.0) / 3.5

    t = max(0.0, min(1.0, t))
    return base - span / 2 + t * span


def _confidence(macro_count: int, sector_count: int) -> str:
    if macro_count == 0:
        return "Baja"
    if macro_count >= 4 and sector_count >= 6:
        return "Alta"
    if macro_count >= 2 or sector_count >= 4:
        return "Media"
    return "Baja"


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def get_business_cycle_phase(api_key: Optional[str] = None) -> dict:
    """
    Detecta la fase del ciclo económico combinando indicadores FRED y rotación sectorial.

    Scoring macro: nivel 70% + delta/tendencia 30% por cada indicador.
    Combinación final: macro 60% + sectorial 40%.

    Returns:
        dict con: phase, phase_label, score, confidence, macro_signals,
                  sector_leaders, sector_results, favorable_sectors,
                  position_pct, marker_x, marker_y, color,
                  scoring_detalle, warning_concentracion
    """
    fred_key = api_key or os.environ.get("FRED_API_KEY") or ""

    macro_score: float = 0.0
    macro_signals: list[dict] = []
    scoring_macro: dict = {}
    macro_count: int = 0

    try:
        macro_score, macro_signals, scoring_macro = _get_macro_signals(fred_key)
        macro_count = sum(1 for s in macro_signals if s["valor"] != "N/D")
    except Exception:
        pass

    sector_score: float = 0.0
    sector_results: list[dict] = []
    scoring_sectorial: dict = {}
    warning_concentracion: Optional[dict] = None

    try:
        sector_score, sector_results, scoring_sectorial, warning_concentracion = _get_sector_rotation()
    except Exception:
        pass

    phase, combined = _determine_phase(macro_score, sector_score)
    position_pct    = _position_for_phase(phase, combined)

    # Coordenadas SVG: x = 30 + 740*t, y = 230 - 170*sin(π*t)
    t        = position_pct
    marker_x = round(30 + 740 * t, 1)
    marker_y = round(230 - 170 * math.sin(math.pi * t), 1)

    # Top 3 sectores con mejor rendimiento relativo
    sector_leaders = sorted(sector_results, key=lambda x: x["retorno_rel"], reverse=True)[:3]

    favorable = [
        {"ticker": s, "nombre": _SECTOR_ETFS[s]["nombre"]}
        for s in _FAVORABLE_SECTORS.get(phase, [])
        if s in _SECTOR_ETFS
    ]

    confianza = _confidence(macro_count, len(sector_results))
    if macro_count == 0 and not sector_results:
        confianza = "Sin datos"

    # ── Detalle de scoring combinado (MEJORA 2) ─────────────────────────────
    scoring_detalle = {
        "macro": scoring_macro,
        "sectorial": scoring_sectorial,
        "combinado": {
            "macro_ponderado":     round(macro_score * 0.6, 4),
            "sectorial_ponderado": round(sector_score * 0.4, 4),
            "score_final":         round(combined, 4),
        },
    }

    return {
        "phase":                 phase,
        "phase_label":           _PHASE_LABELS[phase],
        "score":                 round(combined, 2),
        "confidence":            confianza,
        "macro_signals":         macro_signals,
        "sector_leaders":        sector_leaders,
        "sector_results":        sector_results,
        "favorable_sectors":     favorable,
        "position_pct":          round(position_pct, 4),
        "marker_x":              marker_x,
        "marker_y":              marker_y,
        "color":                 _PHASE_COLORS[phase],
        "scoring_detalle":       scoring_detalle,
        "warning_concentracion": warning_concentracion,
    }
