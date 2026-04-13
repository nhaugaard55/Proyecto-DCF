"""
Detección automática de fase del ciclo económico.

Combina indicadores macro de la API de FRED con rotación sectorial
calculada con yfinance para determinar en qué fase del ciclo se encuentra
la economía: Early Expansion, Late Expansion, Early Contraction, Late Contraction.
"""

import math
import os
from typing import Optional

import requests
import yfinance as yf

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
_FRED_API_KEY_DEFAULT = "03b0d61b2efbea3313f92d4d117af8df"

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
    "early_expansion":   "#22c55e",   # verde
    "late_expansion":    "#84cc16",   # verde amarillento
    "early_contraction": "#f97316",   # naranja
    "late_contraction":  "#ef4444",   # rojo
}


def _fred_latest(series_id: str, api_key: str) -> Optional[float]:
    """Devuelve el valor más reciente de una serie FRED."""
    try:
        resp = requests.get(
            _FRED_BASE,
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 5,
            },
            timeout=8,
        )
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        for o in obs:
            v = o.get("value", ".")
            if v != ".":
                return float(v)
    except Exception:
        pass
    return None


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


def _score_pmi(pmi: Optional[float]) -> float:
    """NAPM (ISM Manufacturing): > 50 expansión."""
    if pmi is None:
        return 0.0
    if pmi >= 55:
        return 2.0
    if pmi >= 50:
        return 1.0
    if pmi >= 45:
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
    """Inflación YoY: moderada es normal, muy alta → late expansion / contraction."""
    if cpi_yoy is None:
        return 0.0
    if cpi_yoy < 2.5:
        return 1.5    # baja inflación → early expansion
    if cpi_yoy < 4.0:
        return 0.5
    if cpi_yoy < 6.0:
        return -0.5   # presión inflacionaria → late expansion o contracción
    return -1.5


def _score_lei(lei: Optional[float]) -> float:
    """USSLIND (LEI): positivo → expansión."""
    if lei is None:
        return 0.0
    if lei > 101:
        return 2.0
    if lei > 99:
        return 1.0
    if lei > 97:
        return -1.0
    return -2.0


def _get_macro_signals(api_key: str) -> tuple[float, list[dict]]:
    """Obtiene y puntúa indicadores macro. Devuelve (score, lista_señales)."""
    t10y2y = _fred_latest("T10Y2Y", api_key)
    napm   = _fred_latest("NAPM", api_key)
    unrate = _fred_latest("UNRATE", api_key)
    lei    = _fred_latest("USSLIND", api_key)

    # CPI YoY: comparamos los dos últimos valores
    cpi_yoy: Optional[float] = None
    try:
        resp = requests.get(
            _FRED_BASE,
            params={
                "series_id": "CPIAUCSL",
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 14,
            },
            timeout=8,
        )
        resp.raise_for_status()
        obs_cpi = [o for o in resp.json().get("observations", []) if o.get("value", ".") != "."]
        if len(obs_cpi) >= 13:
            cpi_now = float(obs_cpi[0]["value"])
            cpi_ago = float(obs_cpi[12]["value"])
            if cpi_ago:
                cpi_yoy = (cpi_now - cpi_ago) / cpi_ago * 100
    except Exception:
        pass

    scores = {
        "Curva de rendimiento (T10Y2Y)": (_score_yield_curve(t10y2y), t10y2y, "%"),
        "ISM Manufacturing (PMI)":        (_score_pmi(napm), napm, ""),
        "Desempleo (UNRATE)":             (_score_unemployment(unrate), unrate, "%"),
        "Inflación CPI (YoY)":            (_score_cpi(cpi_yoy), cpi_yoy, "%"),
        "Índice líder (LEI)":             (_score_lei(lei), lei, ""),
    }

    total = sum(s for s, _, _ in scores.values())
    señales = []
    for nombre, (score, valor, unidad) in scores.items():
        valor_str = f"{valor:.2f}{unidad}" if valor is not None else "N/D"
        señales.append({"nombre": nombre, "score": score, "valor": valor_str})

    return total, señales


def _get_sector_rotation() -> tuple[float, list[dict]]:
    """
    Compara rendimiento a 90 días de ETFs sectoriales vs SPY.
    Devuelve (score_neto, lista_sectores).
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
        return 0.0, []

    if closes.empty or "SPY" not in closes.columns:
        return 0.0, []

    spy_start = closes["SPY"].dropna().iloc[0]
    spy_end   = closes["SPY"].dropna().iloc[-1]
    if spy_start == 0:
        return 0.0, []
    spy_ret = (spy_end - spy_start) / spy_start

    fase_scores: dict[str, float] = {f: 0.0 for f in _PHASE_ORDER}
    resultados = []

    for ticker, info in _SECTOR_ETFS.items():
        if ticker not in closes.columns:
            continue
        serie = closes[ticker].dropna()
        if len(serie) < 5:
            continue
        ret = (serie.iloc[-1] - serie.iloc[0]) / serie.iloc[0]
        relativo = ret - spy_ret
        resultados.append({
            "ticker": ticker,
            "nombre": info["nombre"],
            "fase": _PHASE_LABELS[info["fase"]],
            "retorno_rel": round(relativo * 100, 2),
        })
        # Suma al score de su fase según performance relativa
        if relativo > 0.03:
            fase_scores[info["fase"]] += 2.0
        elif relativo > 0.01:
            fase_scores[info["fase"]] += 1.0
        elif relativo < -0.03:
            fase_scores[info["fase"]] -= 1.0

    # Score neto: fases de expansión suman, de contracción restan
    expansion_score = fase_scores["early_expansion"] + fase_scores["late_expansion"]
    contraction_score = fase_scores["early_contraction"] + fase_scores["late_contraction"]
    net = expansion_score - contraction_score

    # Clamp a [-4, 4] y normalizar a [-2, 2]
    net = max(-4.0, min(4.0, net))
    net = net / 2.0

    return net, resultados


def _determine_phase(macro_score: float, sector_score: float) -> tuple[str, float]:
    """
    Combina macro (60%) y sector (40%) para determinar la fase.
    Devuelve (phase_key, combined_score).
    """
    combined = macro_score * 0.6 + sector_score * 0.4

    # El rango de macro es ~[-10, +10] normalizado a ~[-2, +2] por indicador (5 indicadores)
    # Usamos umbrales sobre el score combinado (escala ~ -6 a +6)
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
    # Ajuste fino según score dentro de la fase
    # Cada fase ocupa ~0.25 del ciclo; limitamos la variación a ±0.08
    span = 0.20
    if phase == "early_expansion":
        # score: 3.5 a 6+  →  0.0 a 0.25
        t = (combined_score - 3.5) / 4.0
    elif phase == "late_expansion":
        # score: 0 a 3.5  →  0.25 a 0.5
        t = combined_score / 3.5
    elif phase == "early_contraction":
        # score: -3.5 a 0  →  0.5 a 0.75
        t = (combined_score + 3.5) / 3.5
    else:
        # late_contraction: -6 a -3.5  →  0.75 a 1.0
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


def get_business_cycle_phase(api_key: Optional[str] = None) -> dict:
    """
    Detecta la fase del ciclo económico combinando indicadores FRED y rotación sectorial.

    Returns:
        dict con: phase, phase_label, score, confidence, macro_signals,
                  sector_leaders, favorable_sectors, position_pct,
                  marker_x, marker_y, color
    """
    fred_key = api_key or os.environ.get("FRED_API_KEY", _FRED_API_KEY_DEFAULT)

    macro_score = 0.0
    macro_signals: list[dict] = []
    macro_available = False

    try:
        macro_score, macro_signals = _get_macro_signals(fred_key)
        macro_count = sum(1 for s in macro_signals if s["valor"] != "N/D")
        macro_available = macro_count > 0
    except Exception:
        macro_count = 0

    sector_score = 0.0
    sector_results: list[dict] = []
    try:
        sector_score, sector_results = _get_sector_rotation()
    except Exception:
        pass

    phase, combined = _determine_phase(macro_score, sector_score)
    position_pct = _position_for_phase(phase, combined)

    # Calcular coordenadas SVG del marcador
    # Curva sinusoidal: x de 30 a 770 (ancho 740), y = 230 - 170*sin(π*t)
    t = position_pct
    marker_x = round(30 + 740 * t, 1)
    marker_y = round(230 - 170 * math.sin(math.pi * t), 1)

    # Sectores líderes: top 3 con mejor rendimiento relativo
    sector_leaders = sorted(sector_results, key=lambda x: x["retorno_rel"], reverse=True)[:3]

    favorable = [
        {"ticker": s, "nombre": _SECTOR_ETFS[s]["nombre"]}
        for s in _FAVORABLE_SECTORS.get(phase, [])
        if s in _SECTOR_ETFS
    ]

    confianza = _confidence(
        sum(1 for s in macro_signals if s["valor"] != "N/D"),
        len(sector_results),
    )

    if not macro_available and not sector_results:
        confianza = "Sin datos"

    return {
        "phase": phase,
        "phase_label": _PHASE_LABELS[phase],
        "score": round(combined, 2),
        "confidence": confianza,
        "macro_signals": macro_signals,
        "sector_leaders": sector_leaders,
        "sector_results": sector_results,
        "favorable_sectors": favorable,
        "position_pct": round(position_pct, 4),
        "marker_x": marker_x,
        "marker_y": marker_y,
        "color": _PHASE_COLORS[phase],
    }
