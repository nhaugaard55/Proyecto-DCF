"""
Detección automática de la etapa del ciclo de vida empresarial.

Implementa el framework "Valuation by Stage" de Brian Feroldi:
  1: Startup
  2: Hyper Growth
  3: Break Even
  4: Operating Leverage
  5: Capital Return
  6: Decline

Usa únicamente los datos financieros que la app ya calcula —
sin llamadas adicionales a APIs externas.
"""

from typing import Optional


# ---------------------------------------------------------------------------
# Metadatos de cada etapa
# ---------------------------------------------------------------------------

STAGE_META = {
    1: {
        "nombre": "Startup",
        "descripcion_breve": "FCF negativo, ingresos bajos, riesgo muy alto.",
        "descripcion": (
            "Empresa en fase inicial. Los ingresos son bajos, el FCF es "
            "negativo y la rentabilidad es lejana. El riesgo es muy alto."
        ),
        "color": "primary",          # Bootstrap color class
        "dcf_utility": "No es útil",
        "dcf_warning": (
            "El DCF produce resultados poco confiables en etapas iniciales. "
            "Con FCFs negativos o inexistentes, las proyecciones son especulativas."
        ),
        "metricas_utiles": ["TAM", "P/S"],
        "metricas_algo_utiles": ["Price / Gross Profit"],
        "metricas_no_utiles": ["P/Forward Earnings", "P/Forward FCF", "P/E", "P/FCF", "DCF", "Reverse DCF"],
    },
    2: {
        "nombre": "Hyper Growth",
        "descripcion_breve": "Revenue crece >20% anual, todavía sin rentabilidad.",
        "descripcion": (
            "La empresa crece ingresos agresivamente (>20–40% anual) pero "
            "aún no es rentable. El foco está en ganar market share."
        ),
        "color": "info",
        "dcf_utility": "No es útil",
        "dcf_warning": (
            "Con FCFs negativos o muy volátiles, el DCF produce valuaciones "
            "inconsistentes. El mercado premia el crecimiento, no los flujos actuales."
        ),
        "metricas_utiles": ["TAM", "P/S", "Price / Gross Profit"],
        "metricas_algo_utiles": [],
        "metricas_no_utiles": ["P/Forward Earnings", "P/Forward FCF", "P/E", "P/FCF", "DCF", "Reverse DCF"],
    },
    3: {
        "nombre": "Break Even",
        "descripcion_breve": "FCF recién positivo, márgenes cerca de cero.",
        "descripcion": (
            "La empresa está cerca del punto de equilibrio. El FCF recién "
            "se vuelve positivo y la rentabilidad empieza a asomar."
        ),
        "color": "warning",
        "dcf_utility": "Algo útil",
        "dcf_warning": (
            "El DCF puede funcionar, pero las proyecciones de FCF aún tienen "
            "alta incertidumbre. Usarlo junto a múltiplos de revenue."
        ),
        "metricas_utiles": ["P/S", "Price / Gross Profit"],
        "metricas_algo_utiles": ["TAM", "P/Forward Earnings", "P/Forward FCF", "DCF", "Reverse DCF"],
        "metricas_no_utiles": ["P/E", "P/FCF"],
    },
    4: {
        "nombre": "Operating Leverage",
        "descripcion_breve": "Márgenes en expansión, FCF crece más que el revenue.",
        "descripcion": (
            "La empresa tiene rentabilidad y escala. Los márgenes se expanden "
            "a medida que crece. El FCF crece más rápido que los ingresos."
        ),
        "color": "success",
        "dcf_utility": "Algo útil",
        "dcf_warning": (
            "En esta etapa el DCF ya aporta señal, pero todavía conviene "
            "contrastarlo con múltiplos forward y de márgenes."
        ),
        "metricas_utiles": ["P/S", "Price / Gross Profit", "P/Forward Earnings", "P/Forward FCF"],
        "metricas_algo_utiles": ["TAM", "P/E", "P/FCF", "DCF", "Reverse DCF"],
        "metricas_no_utiles": [],
    },
    5: {
        "nombre": "Capital Return",
        "descripcion_breve": "FCF predecible, devuelve capital vía dividendos o buybacks.",
        "descripcion": (
            "Empresa madura, rentable y predecible. Genera FCF estable y "
            "lo devuelve a accionistas mediante dividendos o buybacks."
        ),
        "color": "success",
        "dcf_utility": "Útil",
        "dcf_warning": None,
        "metricas_utiles": ["P/Forward Earnings", "P/Forward FCF", "P/E", "P/FCF", "DCF", "Reverse DCF"],
        "metricas_algo_utiles": ["P/S", "Price / Gross Profit"],
        "metricas_no_utiles": ["TAM"],
    },
    6: {
        "nombre": "Decline",
        "descripcion_breve": "Revenue o márgenes cayendo, pérdida de competitividad.",
        "descripcion": (
            "Los ingresos y márgenes se contraen. La empresa pierde relevancia "
            "competitiva. El FCF puede deteriorarse rápidamente."
        ),
        "color": "danger",
        "dcf_utility": "No es útil",
        "dcf_warning": (
            "En declive, los modelos basados en crecimiento y flujos suelen "
            "ser poco representativos. El foco debería pasar al valor de activos o liquidación."
        ),
        "metricas_utiles": [],
        "metricas_algo_utiles": [],
        "metricas_no_utiles": ["TAM", "P/S", "Price / Gross Profit", "P/Forward Earnings", "P/Forward FCF", "P/E", "P/FCF", "DCF", "Reverse DCF"],
    },
}

# Relevancia de las métricas que ya calcula la app, por etapa
# "u" = útil, "a" = algo útil, "n" = no útil
_METRIC_RELEVANCE: dict[str, dict[int, str]] = {
    "P/E":            {1: "n", 2: "n", 3: "n", 4: "a", 5: "u", 6: "n"},
    "P/S":            {1: "u", 2: "u", 3: "u", 4: "u", 5: "a", 6: "n"},
    "P/B":            {1: "u", 2: "u", 3: "a", 4: "a", 5: "a", 6: "n"},
    "ROE":            {1: "n", 2: "n", 3: "a", 4: "u", 5: "u", 6: "n"},
    "Debt/Capital":   {1: "u", 2: "u", 3: "u", 4: "u", 5: "u", 6: "u"},
    "Revenue Growth": {1: "u", 2: "u", 3: "u", 4: "a", 5: "n", 6: "n"},
    "ICR":            {1: "n", 2: "n", 3: "a", 4: "u", 5: "u", 6: "u"},
    "Safety Margin":  {1: "n", 2: "n", 3: "a", 4: "a", 5: "u", 6: "a"},
    "Volumen":        {1: "a", 2: "a", 3: "a", 4: "a", 5: "a", 6: "a"},
}


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _safe_float(value) -> Optional[float]:
    """Convierte a float sin lanzar excepciones."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fcf_values(financials: dict) -> list[float]:
    """
    Extrae los valores de FCF histórico (en miles de millones) más recientes primero.
    Filtra entradas sin valor.
    """
    historico = financials.get("fcf_historico") or []
    result = []
    for entry in historico:
        v = _safe_float(entry.get("valor") if isinstance(entry, dict) else None)
        if v is not None:
            result.append(v)
    return result


def _consec_negative(values: list[float]) -> int:
    """Cuenta años consecutivos con FCF negativo desde el más reciente."""
    count = 0
    for v in values:
        if v < 0:
            count += 1
        else:
            break
    return count


def _is_growing(values: list[float], n: int = 3) -> bool:
    """True si los últimos n valores (más reciente primero) son positivos y crecientes."""
    if len(values) < n:
        return False
    recent = values[:n]
    return all(v >= 0 for v in recent) and recent[0] >= recent[1]


def _fcf_trend_label(values: list[float]) -> str:
    """Etiqueta legible de la tendencia de FCF."""
    if not values:
        return "Sin datos"
    neg = _consec_negative(values)
    if neg >= 3:
        return "Negativo 3+ años"
    if neg >= 1:
        return f"Negativo {neg} año{'s' if neg > 1 else ''}"
    if _is_growing(values, 3):
        return "Positivo y creciendo"
    if all(v >= 0 for v in values[:2]) and any(v < 0 for v in values[2:4]):
        return "Positivo reciente"
    return "Positivo estable"


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def detect_company_stage(ticker: str, financials: dict) -> dict:
    """
    Detecta la etapa del ciclo de vida empresarial usando scoring por señales.

    Parámetros:
        ticker: símbolo bursátil (solo para referencia en el resultado).
        financials: dict con los datos financieros calculados por la app
                    (el dict `resultado` que devuelve analizar_empresa()).

    Retorna:
        dict con stage, stage_name, stage_description, confidence,
        dcf_utility, dcf_warning, signals, useful_metrics, etc.
    """

    scores: dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0}

    # ── Extraer datos ────────────────────────────────────────────────────────
    fcf_vals = _fcf_values(financials)
    consec_neg = _consec_negative(fcf_vals)

    revenue_growth = _safe_float(financials.get("revenue_growth_raw"))
    net_margin     = _safe_float(financials.get("net_margin"))
    has_dividends  = bool(financials.get("has_dividends", False))

    datos_empresa = financials.get("datos_empresa") or {}
    revenue_ttm   = _safe_float(datos_empresa.get("revenue_ttm"))

    # CAGR de FCF (de metricas; puede ser el promedio o CAGR según método elegido)
    metricas = financials.get("metricas") or {}
    cagr_raw = _safe_float(metricas.get("crecimiento_cagr"))

    # ── SEÑAL 1: Free Cash Flow ──────────────────────────────────────────────
    fcf_cagr_display = "N/A"
    if not fcf_vals:
        fcf_trend = "Sin datos"
    elif consec_neg >= 3:
        scores[1] += 3.0
        fcf_trend = _fcf_trend_label(fcf_vals)
    elif consec_neg >= 1:
        scores[2] += 2.0
        fcf_trend = _fcf_trend_label(fcf_vals)
    elif (len(fcf_vals) >= 3
          and all(v >= 0 for v in fcf_vals[:2])
          and any(v < 0 for v in fcf_vals[2:4])):
        # Positivo 1-2 años, antes era negativo → Break Even
        scores[3] += 2.0
        fcf_trend = _fcf_trend_label(fcf_vals)
    elif _is_growing(fcf_vals, 3):
        scores[4] += 2.0
        fcf_trend = _fcf_trend_label(fcf_vals)
    else:
        scores[5] += 2.0
        fcf_trend = _fcf_trend_label(fcf_vals)

    # ── SEÑAL 2: Revenue Growth ──────────────────────────────────────────────
    if revenue_growth is None:
        rev_display = "N/D"
    else:
        rev_display = f"{revenue_growth:.1%}"
        if revenue_growth < 0:
            scores[6] += 3.0
        elif revenue_growth > 0.40:
            scores[2] += 4.0
        elif revenue_growth > 0.20:
            scores[2] += 2.0
            scores[3] += 1.0
        elif revenue_growth > 0.10:
            scores[4] += 2.0
        else:
            scores[5] += 2.0

    # ── SEÑAL 3: Net Margin ──────────────────────────────────────────────────
    if net_margin is None:
        margin_display = "N/D"
    else:
        margin_display = f"{net_margin:.1%}"
        if net_margin < -0.20:
            scores[1] += 2.0
        elif net_margin < 0:
            scores[2] += 1.0
            scores[3] += 1.0
        elif net_margin < 0.05:
            scores[3] += 2.0
        else:
            scores[4] += 2.0
            scores[5] += 1.0

    # ── Calibración Startup vs Hyper Growth ────────────────────────────────
    # FCF negativo y rentabilidad negativa son comunes a Startup e Hyper
    # Growth. Cuando el revenue crece >40% anual, esa señal debe dominar la
    # distinción: la empresa ya está escalando, aunque todavía queme caja.
    # Si además supera $1B de revenue anual, deja de encajar con "ingresos
    # bajos", por lo que Startup queda penalizada.
    if revenue_growth is not None and revenue_growth > 0.40:
        scores[2] += 2.0
        scores[1] = max(0.0, scores[1] - 1.5)

        if revenue_ttm is not None and revenue_ttm >= 1_000_000_000:
            scores[2] += 1.5
            scores[1] = max(0.0, scores[1] - 2.0)

    # ── SEÑAL 4: Dividendos / Buybacks ──────────────────────────────────────
    if has_dividends:
        scores[5] += 3.0

    # ── SEÑAL 5: CAGR de FCF ─────────────────────────────────────────────────
    # Si hay FCF negativos, el CAGR no es interpretable
    if consec_neg >= 1 or not fcf_vals:
        scores[1] += 0.5
        scores[2] += 0.5
        fcf_cagr_display = "N/A (FCF negativo)"
    elif cagr_raw is not None:
        fcf_cagr_display = f"{cagr_raw:.1%}"
        if cagr_raw > 0.50:
            scores[2] += 2.0
        elif cagr_raw > 0.15:
            scores[4] += 2.0
        else:
            scores[5] += 1.0
            scores[6] += 1.0

    # ── SEÑAL 6: FCF Reversal ────────────────────────────────────────────────
    # Si el FCF fue positivo en el pasado y ahora es negativo → Decline, no Startup.
    if fcf_vals and consec_neg >= 2:
        older_vals = fcf_vals[consec_neg:]
        if any(v > 0 for v in older_vals):
            scores[6] += 3.0
            scores[1] = max(0.0, scores[1] - 2.0)

    # ── SEÑAL 7: Longitud del historial de FCF ───────────────────────────────
    # Una startup tiene poco historial; una empresa en decline tiene historial largo.
    n_fcf = len(fcf_vals)
    if n_fcf >= 6:
        scores[6] += 1.5
        scores[1] = max(0.0, scores[1] - 1.5)
    elif n_fcf >= 4:
        scores[6] += 0.5
        scores[1] = max(0.0, scores[1] - 0.5)
    elif n_fcf <= 1:
        scores[1] += 1.0

    # ── SEÑAL 8: Deterioro del FCF ───────────────────────────────────────────
    # Si el FCF se está poniendo más negativo con el tiempo → Decline.
    # Si el FCF mejora (menos negativo) → más Startup/Growth.
    if consec_neg >= 3 and n_fcf >= 4:
        recent_avg  = sum(fcf_vals[:2]) / 2
        older_avg   = sum(fcf_vals[2:4]) / 2
        if recent_avg < older_avg:          # empeorando → Decline
            scores[6] += 2.0
            scores[1] = max(0.0, scores[1] - 1.0)
        else:                               # mejorando → Startup/Growth
            scores[1] += 1.0
            scores[2] += 0.5

    # ── SEÑAL 9: Ausencia total de datos fundamentales + FCF negativo largo ──
    # Una startup genuina normalmente tiene al menos revenue o margen reportado.
    # Empresa con todos los datos N/D y FCF negativo 3+ años → crisis/decline.
    if revenue_growth is None and net_margin is None and consec_neg >= 3 and n_fcf >= 4:
        scores[6] += 1.5
        scores[1] = max(0.0, scores[1] - 1.0)

    # ── Determinar etapa ganadora ────────────────────────────────────────────
    sorted_stages = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_stage, top_score  = sorted_stages[0]
    _, second_score        = sorted_stages[1]

    gap = top_score - second_score
    if top_score == 0:
        confidence = "Sin datos"
    elif gap >= 3:
        confidence = "Alta"
    elif gap >= 1.5:
        confidence = "Media"
    else:
        confidence = "Baja"

    # ── Relevancia de las métricas que ya muestra la app ────────────────────
    filtros_relevancia: list[dict] = []
    for filtro in (financials.get("filtros") or []):
        nombre = filtro.get("nombre", "")
        if nombre == "Safety Margin":
            continue
        relevancia = _METRIC_RELEVANCE.get(nombre, {}).get(top_stage, "a")
        filtros_relevancia.append({
            "nombre": nombre,
            "descripcion": filtro.get("descripcion", ""),
            "valor": filtro.get("valor", "N/D"),
            "criterio": filtro.get("criterio", ""),
            "cumple": filtro.get("cumple"),
            "relevancia": relevancia,   # "u", "a", "n"
        })

    # ── Metadatos de la etapa ────────────────────────────────────────────────
    meta = STAGE_META[top_stage]

    return {
        "stage":             top_stage,
        "stage_name":        meta["nombre"],
        "stage_description": meta["descripcion"],
        "confidence":        confidence,
        "color":             meta["color"],
        "dcf_utility":       meta["dcf_utility"],
        "dcf_warning":       meta["dcf_warning"],
        "scores":            dict(scores),
        "signals": {
            "fcf_trend":      fcf_trend,
            "revenue_growth": rev_display,
            "net_margin":     margin_display,
            "has_dividends":  has_dividends,
            "fcf_cagr":       fcf_cagr_display,
        },
        "filtros_relevancia":    filtros_relevancia,
        "metricas_utiles":       meta["metricas_utiles"],
        "metricas_algo_utiles":  meta["metricas_algo_utiles"],
        "metricas_no_utiles":    meta["metricas_no_utiles"],
    }
