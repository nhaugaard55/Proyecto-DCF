"""
Motor de valuación multi-modelo.

Ejecuta 13 modelos de valuación que producen precio por acción, más
1 métrica auxiliar de solvencia (Altman Z-Score) que no entra al consenso.
Los modelos se ponderan mediante un sistema de pesos adaptativos por etapa (1–6)
detectada por company_stage.py, y producen un precio consenso final.

No realiza llamadas adicionales a APIs — usa exclusivamente los datos
ya presentes en el dict `financials` (resultado de analizar_empresa()).
"""

from __future__ import annotations

import math
from typing import Optional

from .finanzas import G_TERMINAL, calcular_valor_intrinseco, proyectar_fcf

# scipy se importa de forma diferida para que el módulo sea importable
# incluso si scipy no está instalado (en ese caso reverse_dcf = None).
try:
    from scipy.optimize import brentq as _brentq   # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ---------------------------------------------------------------------------
# Ratios sectoriales por defecto
# ---------------------------------------------------------------------------

_SECTOR_RATIOS: dict[str, dict[str, float]] = {
    "Technology":         {"pe": 28.0, "ps": 6.0,  "pgp": 12.0, "pfcf": 25.0, "pe_fwd": 25.0},
    "Healthcare":         {"pe": 22.0, "ps": 4.0,  "pgp": 8.0,  "pfcf": 20.0, "pe_fwd": 20.0},
    "Consumer Cyclical":  {"pe": 24.0, "ps": 1.5,  "pgp": 5.0,  "pfcf": 20.0, "pe_fwd": 21.0},
    "Consumer Defensive": {"pe": 20.0, "ps": 1.0,  "pgp": 4.0,  "pfcf": 18.0, "pe_fwd": 18.0},
    "Financial Services": {"pe": 12.0, "ps": 2.0,  "pgp": 6.0,  "pfcf": 14.0, "pe_fwd": 11.0},
    "Energy":             {"pe": 14.0, "ps": 1.2,  "pgp": 4.0,  "pfcf": 12.0, "pe_fwd": 12.0},
    "Industrials":        {"pe": 20.0, "ps": 1.5,  "pgp": 6.0,  "pfcf": 18.0, "pe_fwd": 18.0},
    "Utilities":          {"pe": 16.0, "ps": 2.0,  "pgp": 5.0,  "pfcf": 14.0, "pe_fwd": 15.0},
    "Basic Materials":    {"pe": 16.0, "ps": 1.5,  "pgp": 5.0,  "pfcf": 15.0, "pe_fwd": 14.0},
    "Real Estate":        {"pe": 30.0, "ps": 5.0,  "pgp": 10.0, "pfcf": 22.0, "pe_fwd": 28.0},
}
_DEFAULT_RATIOS: dict[str, float] = {
    "pe": 20.0, "ps": 3.0, "pgp": 7.0, "pfcf": 18.0, "pe_fwd": 18.0
}

_MARKET_RETURN_FED = 0.08   # long-term equity market return assumption
_YEARS_FED = 5
_MAX_GROWTH_RAW_FED = 0.75  # cap extreme values before reduction table

_SECTOR_EBITDA_MULTIPLES: dict[str, float] = {
    "Technology":             18.0,
    "Healthcare":             14.0,
    "Consumer Cyclical":      12.0,
    "Consumer Defensive":     13.0,
    "Energy":                  7.0,
    "Industrials":            11.0,
    "Financial Services":      9.0,
    "Utilities":              10.0,
    "Real Estate":            16.0,
    "Basic Materials":         9.0,
    "Communication Services": 12.0,
}
_DEFAULT_EBITDA_MULTIPLE = 12.0

_SECTOR_TAM_SCALE: dict[str, float] = {
    "Technology": 1.25,
    "Healthcare": 1.15,
    "Consumer Cyclical": 1.00,
    "Consumer Defensive": 0.85,
    "Financial Services": 0.90,
    "Energy": 0.75,
    "Industrials": 0.95,
    "Utilities": 0.70,
    "Basic Materials": 0.80,
    "Real Estate": 0.85,
}

_STAGE_TAM_ASSUMPTIONS: dict[int, dict[str, float]] = {
    1: {"current_pen": 0.02, "target_pen": 0.10, "execution": 0.45, "ps_capture": 0.55},
    2: {"current_pen": 0.04, "target_pen": 0.12, "execution": 0.60, "ps_capture": 0.65},
    3: {"current_pen": 0.07, "target_pen": 0.14, "execution": 0.72, "ps_capture": 0.80},
    4: {"current_pen": 0.10, "target_pen": 0.18, "execution": 0.82, "ps_capture": 0.90},
    5: {"current_pen": 0.14, "target_pen": 0.18, "execution": 0.90, "ps_capture": 0.95},
    6: {"current_pen": 0.16, "target_pen": 0.12, "execution": 0.60, "ps_capture": 0.70},
}


# ---------------------------------------------------------------------------
# Pesos por etapa del ciclo de vida
# ---------------------------------------------------------------------------

# Matriz alineada con el gráfico "Valuation by Stage" de Brian Feroldi.
# 1.0 = Útil, 0.5 = Algo útil, 0.0 = No útil.
# Luego los pesos se renormalizan sólo entre los modelos aplicables
# que realmente aportan un precio al consenso.
WEIGHTS: dict[int, dict[str, float | bool]] = {
    1: {  # Startup
        "dcf": 0.0, "reverse_dcf": 0.0,
        "pe_trailing": 0.0, "ps": 1.0,
        "pgp": 0.5, "pfcf_trailing": 0.0,
        "ev_ebitda": 0.0, "ddm": 0.0,
        "fwd_earnings": 0.0, "fwd_fcf": 0.0,
        "tam": 0.0, "liquidation_value": 0.0,  # escenario orientativo, fuera del consenso
        "schwab_iv": 0.0,
        "tam_note": True, "asset_note": False,
    },
    2: {  # Hyper Growth
        "dcf": 0.0, "reverse_dcf": 0.0,
        "pe_trailing": 0.0, "ps": 1.0,
        "pgp": 1.0, "pfcf_trailing": 0.0,
        "ev_ebitda": 0.0, "ddm": 0.0,
        "fwd_earnings": 0.0, "fwd_fcf": 0.0,
        "tam": 0.0, "liquidation_value": 0.0,  # escenario orientativo, fuera del consenso
        "schwab_iv": 0.3,
        "tam_note": True, "asset_note": False,
    },
    3: {  # Break Even
        "dcf": 0.5, "reverse_dcf": 0.5,
        "pe_trailing": 0.0, "ps": 1.0,
        "pgp": 1.0, "pfcf_trailing": 0.0,
        "ev_ebitda": 0.3, "ddm": 0.0,
        "fwd_earnings": 0.5, "fwd_fcf": 0.5,
        "tam": 0.0, "liquidation_value": 0.0,  # escenario orientativo, fuera del consenso
        "schwab_iv": 0.5,
        "tam_note": False, "asset_note": False,
    },
    4: {  # Operating Leverage
        "dcf": 0.5, "reverse_dcf": 0.5,
        "pe_trailing": 0.5, "ps": 1.0,
        "pgp": 1.0, "pfcf_trailing": 0.5,
        "ev_ebitda": 0.8, "ddm": 0.3,
        "fwd_earnings": 1.0, "fwd_fcf": 1.0,
        "tam": 0.0, "liquidation_value": 0.0,  # escenario orientativo, fuera del consenso
        "schwab_iv": 0.8,
        "tam_note": False, "asset_note": False,
    },
    5: {  # Capital Return
        "dcf": 1.0, "reverse_dcf": 1.0,
        "pe_trailing": 1.0, "ps": 0.5,
        "pgp": 0.5, "pfcf_trailing": 1.0,
        "ev_ebitda": 1.0, "ddm": 0.8,
        "fwd_earnings": 1.0, "fwd_fcf": 1.0,
        "tam": 0.0, "liquidation_value": 0.0,
        "schwab_iv": 0.8,
        "tam_note": False, "asset_note": False,
    },
    6: {  # Decline — los modelos de crecimiento pierden relevancia
        "dcf": 0.0, "reverse_dcf": 0.0,
        "pe_trailing": 0.0, "ps": 0.0,
        "pgp": 0.0, "pfcf_trailing": 0.0,
        "ev_ebitda": 0.4, "ddm": 0.4,
        "fwd_earnings": 0.0, "fwd_fcf": 0.0,
        "tam": 0.0, "liquidation_value": 0.70,
        "schwab_iv": 0.0,
        "tam_note": False, "asset_note": True,
    },
}

_MODEL_KEYS = ["dcf", "reverse_dcf", "pe_trailing", "ps", "pgp",
               "tam", "pfcf_trailing", "ev_ebitda", "ddm", "fwd_earnings", "fwd_fcf",
               "schwab_iv", "liquidation_value"]

_MODEL_NOMBRES = {
    "dcf":               "DCF",
    "reverse_dcf":       "Reverse DCF",
    "pe_trailing":       "P/E Trailing",
    "ps":                "Price to Sales",
    "pgp":               "Price to Gross Profit",
    "tam":               "TAM asistido",
    "pfcf_trailing":     "P/FCF Trailing",
    "ev_ebitda":         "EV/EBITDA",
    "ddm":               "DDM (Gordon Growth)",
    "fwd_earnings":      "P/E Forward",
    "fwd_fcf":           "P/FCF Forward",
    "schwab_iv":         "Earnings Growth Model",
    "liquidation_value": "Valor de Liquidación",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(value) -> Optional[float]:
    """Convierte a float sin lanzar excepciones. None si inválido."""
    if value is None:
        return None
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _ratios(sector: Optional[str]) -> dict[str, float]:
    """Devuelve los ratios sectoriales correspondientes."""
    if not sector:
        return _DEFAULT_RATIOS
    for key, ratios in _SECTOR_RATIOS.items():
        if key.lower() in (sector or "").lower():
            return ratios
    return _DEFAULT_RATIOS


def _tam_sector_scale(sector: Optional[str]) -> float:
    """Factor heurístico de amplitud TAM según sector."""
    if not sector:
        return 1.0
    for key, scale in _SECTOR_TAM_SCALE.items():
        if key.lower() in (sector or "").lower():
            return scale
    return 1.0


def _to_billions(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value / 1_000_000_000, 2)


def _confianza(n_modelos: int) -> str:
    """Nivel de confianza según cantidad de modelos usados."""
    if n_modelos >= 5:
        return "Alta"
    if n_modelos >= 3:
        return "Media"
    return "Baja"


def _relevancia_desde_peso(peso_raw: float) -> str:
    """Convierte el peso categórico a la etiqueta visible en UI."""
    if peso_raw >= 1.0:
        return "Útil"
    if peso_raw > 0.0:
        return "Algo útil"
    return "No útil"


# ---------------------------------------------------------------------------
# Modelos individuales
# ---------------------------------------------------------------------------

def _dcf_escenario(fcf_ttm: float, cagr: float, wacc: float, deuda_neta: float, acciones: float) -> Optional[float]:
    """Calcula valor por acción para un escenario DCF con los parámetros dados."""
    if fcf_ttm <= 0 or wacc is None or wacc <= 0 or not acciones:
        return None
    proyectado = proyectar_fcf(fcf_ttm, cagr)
    valor_total = calcular_valor_intrinseco(proyectado, wacc)
    if valor_total is None:
        return None
    equity = valor_total - deuda_neta
    valor_por_accion = equity / acciones
    return round(valor_por_accion, 2)


def _modelo_dcf(financials: dict) -> dict:
    """Modelo 1 — Reutiliza el DCF ya calculado por la app."""
    valor = _sf(financials.get("valor_intrinseco"))
    metricas = financials.get("metricas") or {}
    crecimiento_pct = _sf(metricas.get("crecimiento_pct"))
    wacc_pct = _sf(metricas.get("wacc_pct"))
    cagr = _sf(metricas.get("crecimiento_cagr")) or 0.05
    wacc = _sf(metricas.get("wacc")) or 0.08

    datos = financials.get("datos_empresa") or {}
    fcf_ttm = _sf(datos.get("fcf_ttm")) or 0.0
    deuda_neta = _sf(datos.get("deuda_neta")) or 0.0
    acciones = _sf(datos.get("acciones")) or 0.0

    detalle = "Proyección DCF ya calculada"
    if crecimiento_pct is not None and wacc_pct is not None:
        detalle = (
            f"Proyección 5 años con crecimiento {crecimiento_pct:.1f}% "
            f"y WACC {wacc_pct:.2f}%"
        )

    escenarios = {
        "bear": {
            "valor": _dcf_escenario(fcf_ttm, cagr * 0.6, wacc * 1.1, deuda_neta, acciones),
            "cagr_usado": round(cagr * 0.6 * 100, 2),
            "wacc_usado": round(wacc * 1.1 * 100, 2),
        },
        "base": {
            "valor": valor,
            "cagr_usado": round(cagr * 100, 2),
            "wacc_usado": round(wacc * 100, 2),
        },
        "bull": {
            "valor": _dcf_escenario(fcf_ttm, cagr * 1.4, wacc * 0.9, deuda_neta, acciones),
            "cagr_usado": round(cagr * 1.4 * 100, 2),
            "wacc_usado": round(wacc * 0.9 * 100, 2),
        },
    }

    return {
        "valor": valor,
        "aplicable": valor is not None,
        "detalle": detalle,
        "escenarios": escenarios,
    }


def _modelo_reverse_dcf(financials: dict, wacc: float) -> dict:
    """
    Modelo 2 — Reverse DCF.

    Dado el precio actual y el WACC, despeja la tasa de crecimiento
    implícita 'g' que justifica ese precio de mercado.
    Usa scipy.optimize.brentq si está disponible.
    """
    precio = _sf(financials.get("precio_actual"))
    datos_empresa = financials.get("datos_empresa") or {}
    acciones = _sf(datos_empresa.get("acciones"))
    deuda_neta = _sf(datos_empresa.get("deuda_neta"))
    deuda = _sf(datos_empresa.get("deuda")) or 0.0
    fcf_ttm = _sf(datos_empresa.get("fcf_ttm"))
    cagr = _sf((financials.get("metricas") or {}).get("crecimiento_cagr")) or 0.05

    if not _HAS_SCIPY:
        return {"valor": None, "g_implicita": None, "veredicto": None,
                "aplicable": False,
                "detalle": "scipy no disponible para resolver Reverse DCF"}

    if wacc is None:
        return {"valor": None, "g_implicita": None, "veredicto": None,
                "aplicable": False,
                "detalle": "WACC no disponible — Reverse DCF no aplicable"}

    if precio is None or acciones is None or not acciones or fcf_ttm is None:
        return {"valor": None, "g_implicita": None, "veredicto": None,
                "aplicable": False, "detalle": "Datos insuficientes"}

    if fcf_ttm <= 0:
        return {"valor": None, "g_implicita": None, "veredicto": None,
                "aplicable": False,
                "detalle": "FCF negativo — Reverse DCF no aplicable"}

    # Reverse DCF debe partir del enterprise value implícito del equity actual.
    # Usar deuda neta lo vuelve más realista en compañías con mucha caja.
    enterprise_value = precio * acciones + (deuda_neta if deuda_neta is not None else deuda)
    g_terminal = G_TERMINAL  # unificado con el DCF principal (2.5%)

    def _ev_dado_g(g: float) -> float:
        """Calcula enterprise value teórico para una tasa g explícita a 5 años."""
        fcf_proj = [fcf_ttm * (1 + g) ** t for t in range(1, 6)]
        pv_fcf = sum(f / (1 + wacc) ** t for t, f in enumerate(fcf_proj, 1))
        if wacc <= g_terminal:
            return pv_fcf
        vt = fcf_proj[-1] * (1 + g_terminal) / (wacc - g_terminal)
        pv_vt = vt / (1 + wacc) ** 5
        return pv_fcf + pv_vt

    def _objetivo(g: float) -> float:
        return _ev_dado_g(g) - enterprise_value

    lower_bound = -0.50
    upper_candidates = [0.15, 0.25, 0.40, 0.60, 1.00]
    objetivo_low = _objetivo(lower_bound)
    g_impl = None
    high_used = None

    for upper_bound in upper_candidates:
        objetivo_high = _objetivo(upper_bound)
        if objetivo_low == 0:
            g_impl = lower_bound
            high_used = upper_bound
            break
        if objetivo_low * objetivo_high <= 0:
            try:
                g_impl = _brentq(
                    _objetivo,
                    lower_bound,
                    upper_bound,
                    xtol=1e-6,
                    maxiter=300,
                )
                high_used = upper_bound
                break
            except (ValueError, RuntimeError):
                continue

    if g_impl is None:
        if _objetivo(upper_candidates[-1]) < 0:
            detalle = (
                "El precio actual implica un crecimiento del FCF superior al "
                f"{upper_candidates[-1] * 100:.0f}% anual en el período explícito, "
                "fuera del rango razonable configurado para el solver."
            )
        else:
            detalle = (
                "No se pudo converger la solución numérica dentro del rango de "
                "crecimiento explícito configurado."
            )
        return {"valor": None, "g_implicita": None, "veredicto": None,
                "aplicable": False,
                "detalle": detalle}

    # Veredicto: comparar g_implicita con CAGR histórico
    diff = g_impl - cagr
    if diff > 0.05:
        veredicto = "Optimista"
    elif diff < -0.05:
        veredicto = "Conservador"
    else:
        veredicto = "Razonable"

    detalle = (
        f"El precio actual implica crecimiento del FCF al {g_impl*100:.1f}% anual. "
        f"CAGR histórico: {cagr*100:.1f}%. Valuación implícita: {veredicto}. "
        f"Se resolvió con deuda neta y crecimiento explícito permitido hasta {high_used*100:.0f}%."
    )

    return {
        "valor": None,          # No produce un precio; es informativo
        "g_implicita": round(g_impl, 4),
        "g_implicita_pct": round(g_impl * 100, 2),
        "cagr_historico_pct": round(cagr * 100, 2),
        "veredicto": veredicto,
        "aplicable": True,
        "detalle": detalle,
    }


def _modelo_pe_trailing(financials: dict, ratios: dict) -> dict:
    """Modelo 3 — P/E Trailing."""
    datos = financials.get("datos_empresa") or {}
    eps = _sf(datos.get("eps_ttm"))
    pe_sector = ratios["pe"]

    if eps is None or eps <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "EPS negativo o no disponible — P/E no aplicable"}

    valor = eps * pe_sector
    return {
        "valor": round(valor, 2),
        "aplicable": True,
        "pe_sector_ref": pe_sector,
        "detalle": f"EPS TTM ${eps:.2f} × P/E sector {pe_sector:.1f}x",
    }


def _modelo_ps(financials: dict, ratios: dict) -> dict:
    """Modelo 4 — Price to Sales."""
    datos = financials.get("datos_empresa") or {}
    revenue = _sf(datos.get("revenue_ttm"))
    acciones = _sf(datos.get("acciones"))
    ps_sector = ratios["ps"]

    if revenue is None or acciones is None or not acciones:
        return {"valor": None, "aplicable": False,
                "detalle": "Revenue TTM o acciones no disponibles"}

    rps = revenue / acciones
    valor = rps * ps_sector
    return {
        "valor": round(valor, 2),
        "aplicable": True,
        "ps_sector_ref": ps_sector,
        "detalle": f"Revenue/acción ${rps:.2f} × P/S sector {ps_sector:.1f}x",
    }


def _modelo_pgp(financials: dict, ratios: dict) -> dict:
    """Modelo 5 — Price to Gross Profit."""
    datos = financials.get("datos_empresa") or {}
    gp = _sf(datos.get("gross_profit_ttm"))
    acciones = _sf(datos.get("acciones"))
    pgp_sector = ratios["pgp"]

    if gp is None or gp <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "Gross Profit TTM negativo, cero o no disponible — P/GP no aplicable"}
    if acciones is None or not acciones:
        return {"valor": None, "aplicable": False,
                "detalle": "Acciones no disponibles"}

    gps = gp / acciones
    valor = gps * pgp_sector
    return {
        "valor": round(valor, 2),
        "aplicable": True,
        "detalle": f"Gross Profit/acción ${gps:.2f} × P/GP sector {pgp_sector:.1f}x",
    }


def _modelo_tam(financials: dict, ratios: dict, stage: int, wacc: float) -> dict:
    """Modelo 6 — TAM asistido con supuestos sectoriales y por etapa."""
    datos = financials.get("datos_empresa") or {}
    revenue = _sf(datos.get("revenue_ttm"))
    acciones = _sf(datos.get("acciones"))
    sector = datos.get("sector")

    if revenue is None or revenue <= 0 or acciones is None or not acciones:
        return {
            "valor": None,
            "aplicable": False,
            "detalle": "Revenue TTM o acciones no disponibles para estimar TAM.",
        }
    if wacc is None:
        return {
            "valor": None,
            "aplicable": False,
            "detalle": "WACC no disponible — TAM no calculable.",
        }

    assumptions = _STAGE_TAM_ASSUMPTIONS.get(stage, _STAGE_TAM_ASSUMPTIONS[4])
    current_pen = assumptions["current_pen"]
    target_pen = assumptions["target_pen"]
    execution = assumptions["execution"]
    ps_capture = assumptions["ps_capture"]

    sector_scale = _tam_sector_scale(sector)
    tam_estimado = (revenue / current_pen) * sector_scale
    revenue_objetivo = tam_estimado * target_pen * execution
    discount_factor = 1 / ((1 + max(wacc, 0.01)) ** 5)
    revenue_objetivo_desc = revenue_objetivo * discount_factor
    ps_objetivo = ratios["ps"] * ps_capture
    valor = (revenue_objetivo_desc / acciones) * ps_objetivo

    detalle = (
        f"TAM estimado ${_to_billions(tam_estimado):.2f}B con penetración actual "
        f"asumida del {current_pen*100:.1f}%. Se modela capturar {target_pen*100:.1f}% "
        f"del TAM, con descuento de ejecución del {execution*100:.0f}% y P/S objetivo "
        f"de {ps_objetivo:.1f}x, descontado 5 años al WACC."
    )

    return {
        "valor": round(valor, 2),
        "aplicable": False,
        "modo": "escenario",
        "detalle": detalle,
        "tam_estimado_billones": _to_billions(tam_estimado),
        "revenue_objetivo_billones": _to_billions(revenue_objetivo),
        "revenue_objetivo_desc_billones": _to_billions(revenue_objetivo_desc),
        "penetracion_actual_pct": round(current_pen * 100, 2),
        "penetracion_objetivo_pct": round(target_pen * 100, 2),
        "execution_pct": round(execution * 100, 2),
        "discount_factor_pct": round(discount_factor * 100, 2),
        "ps_objetivo": round(ps_objetivo, 2),
        "sector_scale": round(sector_scale, 2),
    }


def _modelo_pfcf_trailing(financials: dict, ratios: dict) -> dict:
    """Modelo 7 — Price to FCF Trailing."""
    datos = financials.get("datos_empresa") or {}
    fcf_ttm = _sf(datos.get("fcf_ttm"))
    acciones = _sf(datos.get("acciones"))
    pfcf_sector = ratios["pfcf"]

    if fcf_ttm is None or acciones is None or not acciones or fcf_ttm <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "FCF TTM negativo o no disponible — P/FCF no aplicable"}

    fcfps = fcf_ttm / acciones
    valor = fcfps * pfcf_sector
    return {
        "valor": round(valor, 2),
        "aplicable": True,
        "detalle": f"FCF/acción ${fcfps:.2f} × P/FCF sector {pfcf_sector:.1f}x",
    }


def _ebitda_multiple(sector: Optional[str]) -> tuple[float, str]:
    """Devuelve (múltiplo EV/EBITDA, nombre_sector) para el sector dado."""
    if sector:
        for key, mult in _SECTOR_EBITDA_MULTIPLES.items():
            if key.lower() in sector.lower():
                return mult, key
    return _DEFAULT_EBITDA_MULTIPLE, "Default"


def _modelo_ev_ebitda(financials: dict) -> dict:
    """Modelo EV/EBITDA — Enterprise Value implícito dividido por EBITDA TTM sectorial."""
    datos = financials.get("datos_empresa") or {}
    ebitda = _sf(datos.get("ebitda_ttm"))
    acciones = _sf(datos.get("acciones"))
    deuda_neta = _sf(datos.get("deuda_neta")) or 0.0
    sector = datos.get("sector")

    if ebitda is None or ebitda <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "EBITDA TTM negativo o no disponible — EV/EBITDA no aplicable"}
    if acciones is None or not acciones:
        return {"valor": None, "aplicable": False,
                "detalle": "Acciones no disponibles"}

    mult, sector_key = _ebitda_multiple(sector)
    ev_estimado = ebitda * mult
    equity_value = ev_estimado - deuda_neta

    if equity_value <= 0:
        return {
            "valor": None,
            "aplicable": False,
            "detalle": (
                f"EBITDA TTM ${_to_billions(ebitda):.2f}B × {mult:.0f}x ({sector_key}) = "
                f"EV ${_to_billions(ev_estimado):.2f}B — Deuda neta ${_to_billions(deuda_neta):.2f}B → "
                f"Equity negativo (deuda excede el EV estimado)"
            ),
        }

    valor = equity_value / acciones
    return {
        "valor": round(valor, 2),
        "aplicable": True,
        "detalle": (
            f"EBITDA TTM ${_to_billions(ebitda):.2f}B × {mult:.0f}x ({sector_key}) = "
            f"EV ${_to_billions(ev_estimado):.2f}B — Deuda neta ${_to_billions(deuda_neta):.2f}B = "
            f"Equity ${_to_billions(equity_value):.2f}B ÷ {acciones / 1e9:.2f}B acciones"
        ),
    }


def _modelo_ddm(financials: dict) -> dict:
    """
    Modelo DDM — Gordon Growth Model.

    P = DPS / (Ke - g)
    Solo aplica cuando la empresa tiene dividendos estables con historial ≥ 2 años.
    """
    datos = financials.get("datos_empresa") or {}
    metricas = financials.get("metricas") or {}
    dividendos = financials.get("dividendos") or {}

    dps = _sf(dividendos.get("annual_dividend"))
    dividend_cagr = _sf(dividendos.get("dividend_cagr"))
    dividend_years = int(dividendos.get("dividend_years") or 0)
    beta = _sf(datos.get("beta"))
    tasa_rf = _sf(metricas.get("tasa_rf"))

    if dps is None or dps <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "Empresa no paga dividendos"}

    if dividend_years < 2 or dividend_cagr is None:
        return {"valor": None, "aplicable": False,
                "detalle": "Historial de dividendos insuficiente (menos de 2 años)"}

    beta_usado = beta if beta is not None else 1.0
    rf = tasa_rf if tasa_rf is not None else 0.045
    ke = rf + beta_usado * (_MARKET_RETURN_FED - rf)

    if ke <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": f"Ke ({ke*100:.2f}%) ≤ 0 — modelo no aplicable"}

    g_raw = dividend_cagr
    g_capped = False
    cap_msg = ""

    # Paso 1: floor en 0 para dividendos estancados
    if g_raw < 0:
        g = 0.0
    else:
        g = g_raw

    # Paso 2: cap absoluto al 10% (dividendo no puede crecer en perpetuidad
    # más que la economía nominal)
    if g > 0.10:
        g = 0.10
        g_capped = True
        cap_msg = f"g capeado al 10% (CAGR histórico {g_raw*100:.1f}%)"

    # Paso 3: cap relativo al 60% de Ke — previene divergencia en empresas
    # defensivas con beta bajo y CAGR de dividendo alto (KO, JNJ, PG, T)
    ke_60pct = ke * 0.60
    if g >= ke_60pct:
        g_capeado_ke = ke * 0.55
        if not g_capped:
            cap_msg = (
                f"g capeado al 55% de Ke para evitar divergencia "
                f"(CAGR histórico era {g_raw*100:.1f}%)"
            )
        else:
            cap_msg = (
                f"g capeado al 55% de Ke (CAGR histórico {g_raw*100:.1f}% → "
                f"10% absoluto → {g_capeado_ke*100:.2f}% relativo)"
            )
        g = g_capeado_ke
        g_capped = True

    if g >= ke:
        return {
            "valor": None,
            "aplicable": False,
            "detalle": (
                f"g ({g*100:.2f}%) ≥ Ke ({ke*100:.2f}%): el modelo diverge con estos parámetros"
            ),
        }

    spread = ke - g
    if spread < 0.01:
        return {
            "valor": None,
            "aplicable": False,
            "detalle": (
                f"Ke − g = {spread*100:.2f}% < 1%: denominador demasiado pequeño "
                f"para producir un valor estable"
            ),
        }

    valor = dps / spread
    g_detalle = (
        f"g calculada con CAGR de {dividend_years} años: {g_raw*100:.2f}%"
        + (f" → capeado a {g*100:.2f}% ({cap_msg})" if g_capped else "")
    )
    detalle = (
        f"DPS ${dps:.4f} ÷ (Ke {ke*100:.2f}% − g {g*100:.2f}%) = "
        f"${dps:.4f} ÷ {spread*100:.2f}% = ${valor:.2f}. "
        f"Ke = rf {rf*100:.2f}% + β {beta_usado:.2f} × "
        f"({_MARKET_RETURN_FED*100:.0f}% − {rf*100:.2f}%). "
        + g_detalle
        + (" | β=1.0 asumido (dato no disponible)" if beta is None else "")
    )

    return {
        "valor": round(valor, 2),
        "aplicable": True,
        "dps": dps,
        "ke_pct": round(ke * 100, 2),
        "g_pct": round(g * 100, 2),
        "g_raw_pct": round(g_raw * 100, 2),
        "g_capped": g_capped,
        "spread_pct": round(spread * 100, 2),
        "dividend_years": dividend_years,
        "detalle": detalle,
    }


def _modelo_fwd_earnings(financials: dict, ratios: dict) -> dict:
    """Modelo 8 — Price to Forward Earnings."""
    datos = financials.get("datos_empresa") or {}
    eps_fwd = _sf(datos.get("eps_forward"))
    pe_fwd_sector = ratios.get("pe_fwd", ratios["pe"])

    # Fallback: eps_ttm * (1 + revenue_growth)
    if eps_fwd is None:
        eps_ttm = _sf(datos.get("eps_ttm"))
        rev_growth = _sf((financials.get("metricas") or {}).get("crecimiento_cagr")) or 0.0
        if eps_ttm is not None and eps_ttm > 0:
            eps_fwd = eps_ttm * (1 + rev_growth)
            fuente_eps = f"estimado (EPS TTM ${eps_ttm:.2f} × {1+rev_growth:.2f})"
        else:
            return {"valor": None, "aplicable": False,
                    "detalle": "EPS forward no disponible y EPS TTM negativo"}
    else:
        fuente_eps = f"analistas ${eps_fwd:.2f}"

    if eps_fwd <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "EPS forward negativo — modelo no aplicable"}

    valor = eps_fwd * pe_fwd_sector
    return {
        "valor": round(valor, 2),
        "aplicable": True,
        "detalle": f"EPS forward {fuente_eps} × P/E fwd sector {pe_fwd_sector:.1f}x",
    }


def _modelo_fwd_fcf(financials: dict, ratios: dict) -> dict:
    """Modelo 9 — Price to Forward FCF."""
    datos = financials.get("datos_empresa") or {}
    fcf_ttm = _sf(datos.get("fcf_ttm"))
    acciones = _sf(datos.get("acciones"))
    pfcf_sector = ratios["pfcf"]
    net_margin = _sf(financials.get("net_margin"))
    rev_growth = _sf((financials.get("metricas") or {}).get("crecimiento_cagr")) or 0.05

    if fcf_ttm is None or acciones is None or not acciones or fcf_ttm <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "FCF TTM negativo o no disponible — Forward P/FCF no aplicable"}

    if net_margin is None or net_margin <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "Margen neto negativo — Forward P/FCF no estimable con pérdidas"}

    # FCF forward estimado con ajuste de margen
    margen_fcf = net_margin
    ajuste = 1 + (rev_growth * margen_fcf)
    fcf_fwd = fcf_ttm * ajuste
    fcfps_fwd = fcf_fwd / acciones
    valor = fcfps_fwd * pfcf_sector

    return {
        "valor": round(valor, 2),
        "aplicable": True,
        "detalle": (
            f"FCF forward estimado ${fcfps_fwd:.2f}/acción "
            f"(FCF TTM ajustado × {ajuste:.3f}) × P/FCF sector {pfcf_sector:.1f}x"
        ),
    }


def _fed_reduction(value: float) -> float:
    """
    Tabla de reducción del Earnings Growth Model.
    Para growth: value es el porcentaje (ej. 25.0 para 25%).
    Para sector P/E: value es el múltiplo directo (ej. 55.35).
    Devuelve la fracción de reducción (0.0 – 0.40).
    """
    if value < 6:    return 0.0
    elif value < 12: return 0.05
    elif value < 20: return 0.10
    elif value < 30: return 0.15
    elif value < 35: return 0.20
    elif value < 40: return 0.225
    elif value < 45: return 0.25
    elif value < 50: return 0.275
    elif value < 55: return 0.30
    elif value < 60: return 0.325
    elif value < 65: return 0.35
    elif value < 70: return 0.375
    else:            return 0.40


def _modelo_schwab_iv(financials: dict, ratios: dict) -> dict:
    """
    Modelo — Earnings Growth Model (PEG-CAPM Hybrid).

    Fórmula: IV = (EPS_ttm × (1+g_adj)^N × PE_adj) / (1+r_capm)^N
    Penaliza crecimientos y múltiplos altos con una tabla de reducción progresiva.
    """
    datos = financials.get("datos_empresa") or {}
    metricas = financials.get("metricas") or {}

    eps_ttm = _sf(datos.get("eps_ttm"))
    eps_growth_5y = _sf(datos.get("eps_growth_5y"))
    beta = _sf(datos.get("beta"))
    tasa_rf = _sf(metricas.get("tasa_rf"))

    if eps_ttm is None or eps_ttm <= 0:
        return {
            "valor": None, "aplicable": False,
            "razon_no_aplicable": "EPS negativo o no disponible",
            "detalle": "El modelo requiere EPS TTM positivo",
        }

    if eps_growth_5y is None:
        return {
            "valor": None, "aplicable": False,
            "razon_no_aplicable": "Tasa de crecimiento EPS no calculable",
            "detalle": "Historia insuficiente para estimar CAGR de EPS a 5 años",
        }

    beta_usado = beta if beta is not None else 1.0
    beta_warning = beta is None
    rf = tasa_rf if tasa_rf is not None else 0.045
    # P/E sectorial: mismo valor que usa el modelo P/E Trailing (fuente única)
    sector_pe = ratios["pe"]

    # Capear crecimiento extremo antes del lookup
    g_raw = min(eps_growth_5y, _MAX_GROWTH_RAW_FED)

    # Ajustar growth rate con tabla de reducción
    g_pct = g_raw * 100
    g_reduction = _fed_reduction(g_pct)
    g_adj = min(g_raw * (1 - g_reduction), 0.40)

    # Ajustar sector P/E con misma tabla (input es el P/E directo, no un %)
    pe_reduction = _fed_reduction(sector_pe)
    pe_adj = min(sector_pe * (1 - pe_reduction), 40.0)

    # CAPM
    r_capm = rf + beta_usado * (_MARKET_RETURN_FED - rf)

    # Valor intrínseco
    N = _YEARS_FED
    try:
        eps_projected = eps_ttm * (1 + g_adj) ** N
        price_future = eps_projected * pe_adj
        iv = price_future / (1 + r_capm) ** N
    except (ZeroDivisionError, OverflowError):
        return {
            "valor": None, "aplicable": False,
            "razon_no_aplicable": "Error aritmético",
            "detalle": "No se pudo calcular el valor intrínseco",
        }

    iv = round(iv, 2)
    eps_growth_fuente = datos.get("eps_growth_5y_fuente") or ""

    detalle = (
        f"IV = EPS TTM ${eps_ttm:.2f} × (1+{g_adj*100:.2f}%)^{N} × {pe_adj:.2f}x "
        f"÷ (1+{r_capm*100:.2f}%)^{N} = ${iv:.2f}. "
        f"Sector P/E {sector_pe:.1f}x ajustado → {pe_adj:.2f}x (−{pe_reduction*100:.1f}%). "
        f"g {eps_growth_5y*100:.2f}% ajustado → {g_adj*100:.2f}% (−{g_reduction*100:.1f}%). "
        f"r_CAPM = {rf*100:.2f}% + {beta_usado:.2f}×({_MARKET_RETURN_FED*100:.0f}%−{rf*100:.2f}%) = {r_capm*100:.2f}%"
        + (f". Fuente growth: {eps_growth_fuente}" if eps_growth_fuente else "")
        + (" | β=1.0 asumido (dato no disponible)" if beta_warning else "")
    )

    return {
        "valor": iv,
        "aplicable": True,
        "razon_no_aplicable": None,
        "eps_ttm": eps_ttm,
        "eps_growth_5y_raw_pct": round(eps_growth_5y * 100, 2),
        "g_adj_pct": round(g_adj * 100, 2),
        "g_reduction_pct": round(g_reduction * 100, 1),
        "sector_pe_raw": round(sector_pe, 2),
        "pe_adj": round(pe_adj, 2),
        "pe_reduction_pct": round(pe_reduction * 100, 1),
        "r_capm_pct": round(r_capm * 100, 2),
        "r_capm_rf_pct": round(rf * 100, 2),
        "r_capm_beta": round(beta_usado, 2),
        "beta_warning": beta_warning,
        "eps_growth_fuente": eps_growth_fuente,
        "n_years": N,
        "detalle": detalle,
    }


def _modelo_liquidation_value(financials: dict) -> dict:
    """Modelo 10 — Valor de Liquidación (Benjamin Graham Net-Net)."""
    datos = financials.get("datos_empresa") or {}
    current_assets = _sf(datos.get("total_current_assets"))
    total_liab = _sf(datos.get("total_liabilities"))
    acciones = _sf(datos.get("acciones"))

    if current_assets is None or total_liab is None or not acciones:
        return {
            "valor": None,
            "ncav": None,
            "ncav_billones": None,
            "current_assets_billones": None,
            "total_liab_billones": None,
            "veredicto": None,
            "veredicto_descripcion": None,
            "veredicto_zona_altman": None,
            "aplicable": False,
            "detalle": "Activos corrientes o pasivos totales no disponibles",
        }

    ncav = current_assets - total_liab
    valor_por_accion = ncav / acciones
    ncav_b = _to_billions(ncav)
    ca_b = _to_billions(current_assets)
    tl_b = _to_billions(total_liab)

    precio_actual = _sf(financials.get("precio_actual")) or 0.0
    if ncav <= 0:
        # Veredicto provisional — se refina en run_all_models cruzando con el Z-Score
        veredicto = "Insolvente"
        veredicto_descripcion = "Los pasivos totales superan los activos corrientes"
    elif precio_actual and valor_por_accion >= precio_actual:
        veredicto = "Deep Value (Net-Net)"
        veredicto_descripcion = "El precio cotiza por debajo del NCAV — zona de máximo valor (Graham)"
    else:
        veredicto = "Por encima del valor de liquidación"
        veredicto_descripcion = "El precio de mercado supera el valor de liquidación estimado"

    return {
        "valor": round(valor_por_accion, 2),
        "ncav": ncav,
        "ncav_billones": ncav_b,
        "current_assets_billones": ca_b,
        "total_liab_billones": tl_b,
        "veredicto": veredicto,
        "veredicto_descripcion": veredicto_descripcion,
        "veredicto_zona_altman": None,
        "aplicable": ncav > 0,
        "detalle": (
            f"NCAV = Activos corrientes ${ca_b:.2f}B "
            f"− Pasivos totales ${tl_b:.2f}B = "
            f"${ncav_b:.2f}B ÷ {acciones:,.0f} acciones"
        ),
    }


def _modelo_altman_z_score(financials: dict) -> dict:
    """Altman Z-Score — métrica de solvencia (informativo, no entra al consenso)."""
    datos = financials.get("datos_empresa") or {}
    total_assets = _sf(datos.get("total_assets"))
    working_capital = _sf(datos.get("working_capital"))
    retained_earnings = _sf(datos.get("retained_earnings"))
    ebit = _sf(datos.get("ebit"))
    market_cap = _sf(datos.get("market_cap"))
    total_liab = _sf(datos.get("total_liabilities"))
    revenue = _sf(datos.get("revenue_ttm"))

    campos_faltantes = []
    for nombre, val in [
        ("Total Assets", total_assets),
        ("Working Capital", working_capital),
        ("Retained Earnings", retained_earnings),
        ("EBIT", ebit),
        ("Market Cap", market_cap),
        ("Total Liabilities", total_liab),
        ("Revenue TTM", revenue),
    ]:
        if val is None:
            campos_faltantes.append(nombre)

    if campos_faltantes:
        return {
            "disponible": False,
            "z_score": None,
            "zona": None,
            "zona_code": None,
            "interpretacion": None,
            "componentes": {},
            "detalle": f"Datos insuficientes: {', '.join(campos_faltantes)}",
        }

    if total_assets == 0 or total_liab == 0:
        return {
            "disponible": False,
            "z_score": None,
            "zona": None,
            "zona_code": None,
            "interpretacion": None,
            "componentes": {},
            "detalle": "Total Assets o Total Liabilities es cero",
        }

    x1 = working_capital / total_assets
    x2 = retained_earnings / total_assets
    x3 = ebit / total_assets
    x4 = market_cap / total_liab
    x5 = revenue / total_assets

    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5

    if z > 2.99:
        zona = "Segura"
        zona_code = "safe"
        interpretacion = "Baja probabilidad de quiebra en los próximos 2 años"
    elif z >= 1.81:
        zona = "Zona Gris"
        zona_code = "grey"
        interpretacion = "Zona de incertidumbre — monitorear de cerca"
    else:
        zona = "Distress"
        zona_code = "distress"
        interpretacion = "Alta probabilidad de insolvencia — riesgo severo"

    z_position_pct = round(min(max(z / 5 * 100, 0), 100), 1)

    return {
        "disponible": True,
        "z_score": round(z, 2),
        "z_position_pct": z_position_pct,
        "zona": zona,
        "zona_code": zona_code,
        "interpretacion": interpretacion,
        "componentes": {
            "x1": round(x1, 4), "x1_pct": round(x1 * 100, 2),
            "x2": round(x2, 4), "x2_pct": round(x2 * 100, 2),
            "x3": round(x3, 4), "x3_pct": round(x3 * 100, 2),
            "x4": round(x4, 4), "x4_pct": round(x4 * 100, 2),
            "x5": round(x5, 4), "x5_pct": round(x5 * 100, 2),
        },
        "detalle": (
            f"Z = 1.2×{x1:.3f} + 1.4×{x2:.3f} + "
            f"3.3×{x3:.3f} + 0.6×{x4:.3f} + 1.0×{x5:.3f} = {z:.2f}"
        ),
    }


# ---------------------------------------------------------------------------
# Redistribución de pesos
# ---------------------------------------------------------------------------

def _redistribuir_pesos(
    raw_weights: dict[str, float],
    resultados: dict[str, dict],
) -> dict[str, float]:
    """
    Redistribuye proporcionalmente el peso de modelos no aplicables
    entre los modelos aplicables con peso > 0.
    """
    # Pesos base (sólo los que tienen weight > 0 Y son aplicables)
    utiles: dict[str, float] = {}
    for key in _MODEL_KEYS:
        w = raw_weights.get(key, 0.0)
        if not isinstance(w, (int, float)):
            continue
        r = resultados.get(key, {})
        valor = r.get("valor")
        aplicable = r.get("aplicable", False)
        # reverse_dcf es especial: no produce valor de precio, se excluye del consenso
        if key == "reverse_dcf":
            continue
        if w > 0 and aplicable and valor is not None:
            utiles[key] = float(w)

    total = sum(utiles.values())
    if total == 0:
        return {k: 0.0 for k in _MODEL_KEYS}

    return {k: (utiles.get(k, 0.0) / total) for k in _MODEL_KEYS}


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def run_all_models(
    ticker: str,
    financials: dict,
    stage: int,
    wacc: float,
) -> dict:
    """
    Ejecuta los 13 modelos de valuación y calcula el precio consenso ponderado.

    Parámetros:
        ticker:     Símbolo bursátil (solo para contexto en el retorno).
        financials: Dict completo devuelto por analizar_empresa().
        stage:      Etapa del ciclo de vida (1–6) detectada por company_stage.
        wacc:       WACC calculado por la app (float, ej: 0.082).

    Retorna:
        Dict con "modelos", "consenso" y "stage_context".
    """
    stage = max(1, min(6, int(stage or 4)))
    sector = (financials.get("datos_empresa") or {}).get("sector")
    ratios = _ratios(sector)
    raw_weights = WEIGHTS.get(stage, WEIGHTS[4])
    precio_actual = _sf(financials.get("precio_actual")) or 0.0

    # ── Ejecutar modelos ──────────────────────────────────────────────────────
    resultados_raw: dict[str, dict] = {
        "dcf":               _modelo_dcf(financials),
        "reverse_dcf":       _modelo_reverse_dcf(financials, wacc),
        "pe_trailing":       _modelo_pe_trailing(financials, ratios),
        "ps":                _modelo_ps(financials, ratios),
        "pgp":               _modelo_pgp(financials, ratios),
        "tam":               _modelo_tam(financials, ratios, stage, wacc),
        "pfcf_trailing":     _modelo_pfcf_trailing(financials, ratios),
        "ev_ebitda":         _modelo_ev_ebitda(financials),
        "ddm":               _modelo_ddm(financials),
        "fwd_earnings":      _modelo_fwd_earnings(financials, ratios),
        "fwd_fcf":           _modelo_fwd_fcf(financials, ratios),
        "schwab_iv":         _modelo_schwab_iv(financials, ratios),
        "liquidation_value": _modelo_liquidation_value(financials),
    }

    # Altman Z-Score: informativo, no entra al consenso
    altman = _modelo_altman_z_score(financials)

    # Cruzar Liquidation Value con Z-Score cuando NCAV < 0
    # Un NCAV negativo puede ser estrés real (Intel) o capital optimization (Apple, HD)
    lv_raw = resultados_raw["liquidation_value"]
    if lv_raw.get("ncav") is not None and lv_raw["ncav"] < 0:
        if altman.get("disponible"):
            z = altman["z_score"]
            if z > 2.99:
                lv_raw["veredicto"] = "Estructura de capital optimizada"
                lv_raw["veredicto_descripcion"] = (
                    f"El NCAV negativo refleja una política deliberada de recompra de acciones "
                    f"financiada con deuda — estrategia típica de empresas en Capital Return. "
                    f"El Z-Score ({z}) confirma solidez financiera sin riesgo real de insolvencia."
                )
                lv_raw["veredicto_zona_altman"] = "safe"
            elif z >= 1.81:
                lv_raw["veredicto"] = "Apalancamiento elevado — monitorear"
                lv_raw["veredicto_descripcion"] = (
                    f"El NCAV negativo con Z-Score en zona gris ({z}) sugiere un nivel de deuda "
                    f"elevado que puede ser manejable pero requiere seguimiento de la evolución financiera."
                )
                lv_raw["veredicto_zona_altman"] = "grey"
            else:
                lv_raw["veredicto"] = "Riesgo de insolvencia real"
                lv_raw["veredicto_descripcion"] = (
                    f"El NCAV negativo combinado con Z-Score en zona de distress ({z}) indica "
                    f"riesgo financiero real. Los pasivos totales superan los activos corrientes "
                    f"y la solvencia general de la empresa está comprometida."
                )
                lv_raw["veredicto_zona_altman"] = "distress"
        else:
            lv_raw["veredicto_zona_altman"] = "unknown"

    # ── Pesos ajustados ───────────────────────────────────────────────────────
    pesos_ajustados = _redistribuir_pesos(raw_weights, resultados_raw)

    # ── Construir dict de modelos con metadatos completos ────────────────────
    modelos: dict[str, dict] = {}
    for key in _MODEL_KEYS:
        r = resultados_raw[key]
        peso_raw = float(raw_weights.get(key, 0.0)) if isinstance(raw_weights.get(key), (int, float)) else 0.0
        peso_final = pesos_ajustados.get(key, 0.0)

        relevancia = _relevancia_desde_peso(peso_raw)

        valor_modelo = r.get("valor")
        if valor_modelo is not None and precio_actual and key != "reverse_dcf":
            upside_pct = round((valor_modelo - precio_actual) / precio_actual * 100, 1)
        else:
            upside_pct = None

        entry: dict = {
            "nombre": _MODEL_NOMBRES[key],
            "valor": valor_modelo,
            "upside_pct": upside_pct,
            "precio_actual": precio_actual,
            "peso_raw": round(peso_raw, 4),
            "peso": round(peso_final, 4),
            "peso_pct": round(peso_final * 100, 1),
            "relevancia": relevancia,
            "aplicable": r.get("aplicable", False),
            "detalle": r.get("detalle", ""),
        }

        if key == "dcf":
            entry["escenarios"] = r.get("escenarios")
        elif key == "reverse_dcf":
            entry["g_implicita"] = r.get("g_implicita")
            entry["g_implicita_pct"] = r.get("g_implicita_pct")
            entry["cagr_historico_pct"] = r.get("cagr_historico_pct")
            entry["veredicto"] = r.get("veredicto")
        elif key == "tam":
            entry["modo"] = r.get("modo")
            entry["tam_estimado_billones"] = r.get("tam_estimado_billones")
            entry["revenue_objetivo_billones"] = r.get("revenue_objetivo_billones")
            entry["revenue_objetivo_desc_billones"] = r.get("revenue_objetivo_desc_billones")
            entry["penetracion_actual_pct"] = r.get("penetracion_actual_pct")
            entry["penetracion_objetivo_pct"] = r.get("penetracion_objetivo_pct")
            entry["execution_pct"] = r.get("execution_pct")
            entry["discount_factor_pct"] = r.get("discount_factor_pct")
            entry["ps_objetivo"] = r.get("ps_objetivo")
            entry["sector_scale"] = r.get("sector_scale")
        elif key == "pe_trailing":
            entry["pe_sector_ref"] = r.get("pe_sector_ref")
        elif key == "ps":
            entry["ps_sector_ref"] = r.get("ps_sector_ref")
        elif key == "ddm":
            entry["dps"] = r.get("dps")
            entry["ke_pct"] = r.get("ke_pct")
            entry["g_pct"] = r.get("g_pct")
            entry["g_raw_pct"] = r.get("g_raw_pct")
            entry["g_capped"] = r.get("g_capped")
            entry["spread_pct"] = r.get("spread_pct")
            entry["dividend_years"] = r.get("dividend_years")
        elif key == "schwab_iv":
            entry["eps_ttm"] = r.get("eps_ttm")
            entry["eps_growth_5y_raw_pct"] = r.get("eps_growth_5y_raw_pct")
            entry["g_adj_pct"] = r.get("g_adj_pct")
            entry["g_reduction_pct"] = r.get("g_reduction_pct")
            entry["sector_pe_raw"] = r.get("sector_pe_raw")
            entry["pe_adj"] = r.get("pe_adj")
            entry["pe_reduction_pct"] = r.get("pe_reduction_pct")
            entry["r_capm_pct"] = r.get("r_capm_pct")
            entry["r_capm_rf_pct"] = r.get("r_capm_rf_pct")
            entry["r_capm_beta"] = r.get("r_capm_beta")
            entry["beta_warning"] = r.get("beta_warning")
            entry["eps_growth_fuente"] = r.get("eps_growth_fuente")
            entry["n_years"] = r.get("n_years")
            entry["razon_no_aplicable"] = r.get("razon_no_aplicable")
        elif key == "liquidation_value":
            entry["ncav_billones"] = r.get("ncav_billones")
            entry["current_assets_billones"] = r.get("current_assets_billones")
            entry["total_liab_billones"] = r.get("total_liab_billones")
            entry["veredicto"] = r.get("veredicto")
            entry["veredicto_descripcion"] = r.get("veredicto_descripcion")
            entry["veredicto_zona_altman"] = r.get("veredicto_zona_altman")

        modelos[key] = entry

    # ── Calcular precio consenso ──────────────────────────────────────────────
    valores_aplicables = [
        m["valor"] for m in modelos.values()
        if m["valor"] is not None and m["peso"] > 0
    ]

    if valores_aplicables:
        precio_consenso = sum(
            modelos[k]["valor"] * modelos[k]["peso"]
            for k in _MODEL_KEYS
            if modelos[k]["valor"] is not None and modelos[k]["peso"] > 0
        )
        precio_consenso = round(precio_consenso, 2)
        valor_min = round(min(valores_aplicables), 2)
        valor_max = round(max(valores_aplicables), 2)

        upside_pct = (
            (precio_consenso - precio_actual) / precio_actual * 100
            if precio_actual else None
        )
        upside_pct = round(upside_pct, 1) if upside_pct is not None else None

        if upside_pct is not None:
            if upside_pct > 15:
                veredicto_final = "Subvaluada"
            elif upside_pct < -15:
                veredicto_final = "Sobrevaluada"
            else:
                veredicto_final = "Precio Razonable"
        else:
            veredicto_final = None

        modelos_usados = [k for k in _MODEL_KEYS if modelos[k]["valor"] is not None and modelos[k]["peso"] > 0]
        modelos_excluidos = [k for k in _MODEL_KEYS if modelos[k]["peso"] == 0 or not modelos[k]["aplicable"]]

        variance = sum(
            modelos[k]["peso"] * (modelos[k]["valor"] - precio_consenso) ** 2
            for k in modelos_usados
        )
        dr = math.sqrt(variance) / precio_consenso if precio_consenso else None
        if dr is None:
            dr_label, dr_color = None, None
        elif dr < 0.10:
            dr_label, dr_color = "Alta consistencia entre modelos", "success"
        elif dr <= 0.25:
            dr_label, dr_color = "Consistencia moderada", "warning"
        else:
            dr_label, dr_color = "Alta dispersión — consenso poco confiable", "danger"

        consenso = {
            "precio": precio_consenso,
            "precio_actual": precio_actual,
            "upside_pct": upside_pct,
            "rango_min": valor_min,
            "rango_max": valor_max,
            "veredicto": veredicto_final,
            "modelos_usados": len(modelos_usados),
            "modelos_usados_keys": modelos_usados,
            "modelos_en_consenso": modelos_usados,
            "modelos_excluidos": modelos_excluidos,
            "confianza": _confianza(len(modelos_usados)),
            "disagreement_ratio": round(dr, 4) if dr is not None else None,
            "disagreement_ratio_pct": round(dr * 100, 1) if dr is not None else None,
            "disagreement_label": dr_label,
            "disagreement_color": dr_color,
            "disponible": True,
            "razon_no_calculable": None,
        }
    else:
        razon_no_calculable = None
        if stage == 6:
            razon_no_calculable = (
                "En etapa de Decline, los modelos de flujo de caja y múltiplos no son representativos. "
                "El Valor de Liquidación negativo indica que los pasivos totales superan los activos "
                "corrientes — en un escenario de liquidación, los accionistas no recuperarían capital."
            )
        consenso = {
            "precio": None,
            "precio_actual": precio_actual,
            "upside_pct": None,
            "rango_min": None,
            "rango_max": None,
            "veredicto": None,
            "modelos_usados": 0,
            "modelos_usados_keys": [],
            "modelos_excluidos": list(_MODEL_KEYS),
            "confianza": "Sin datos",
            "disponible": False,
            "razon_no_calculable": razon_no_calculable,
        }

    # ── Contexto de etapa ─────────────────────────────────────────────────────
    from .company_stage import STAGE_META  # import diferido para evitar circular
    meta = STAGE_META.get(stage, STAGE_META[4])

    nota_especial: Optional[str] = None
    if raw_weights.get("tam_note"):
        nota_especial = (
            "En etapas tempranas el consenso depende más de la oportunidad de mercado. "
            "El modelo TAM asistido y la trayectoria de crecimiento pesan más que los flujos actuales."
        )
    elif raw_weights.get("asset_note"):
        nota_especial = (
            "En etapa de declive los modelos de flujo de caja tienen utilidad "
            "limitada. Considerá el valor de liquidación y los dividendos."
        )

    utiles_keys = [k for k in _MODEL_KEYS if float(raw_weights.get(k, 0) or 0) >= 1.0]
    algo_utiles_keys = [k for k in _MODEL_KEYS if 0.0 < float(raw_weights.get(k, 0) or 0) < 1.0]
    no_utiles_keys = [k for k in _MODEL_KEYS if float(raw_weights.get(k, 0) or 0) == 0]

    stage_context = {
        "stage": stage,
        "stage_name": meta["nombre"],
        "modelos_utiles": [_MODEL_NOMBRES[k] for k in utiles_keys],
        "modelos_algo_utiles": [_MODEL_NOMBRES[k] for k in algo_utiles_keys],
        "modelos_no_utiles": [_MODEL_NOMBRES[k] for k in no_utiles_keys],
        "nota_especial": nota_especial,
    }

    score_final = calcular_score_final(consenso, altman, financials.get("filtros"), stage)

    return {
        "ticker": ticker,
        "modelos": modelos,
        "consenso": consenso,
        "stage_context": stage_context,
        "altman": altman,
        "score_final": score_final,
    }


def calcular_score_final(consenso_dict, altman_dict, filtros_dict, stage) -> dict:
    """
    Calcula un score final de inversión de 0 a 10 a partir del consenso
    multi-modelo, la dispersión entre modelos, la solvencia y los filtros
    fundamentales.

    La función no depende de Django ni realiza llamadas externas. Si algún
    componente no está disponible, asigna puntaje neutro para no castigar ni
    premiar datos faltantes.
    """
    consenso = consenso_dict or {}
    altman = altman_dict or {}
    try:
        stage_num = int(stage or 4)
    except (TypeError, ValueError):
        stage_num = 4

    modelos_usados = consenso.get("modelos_usados")
    try:
        modelos_usados_count = int(modelos_usados or 0)
    except (TypeError, ValueError):
        modelos_usados_count = 0

    precio_consenso = _sf(consenso.get("precio"))
    precio_actual = _sf(consenso.get("precio_actual"))
    consenso_disponible = bool(consenso.get("disponible")) and modelos_usados_count >= 2

    if not consenso_disponible or precio_consenso is None or not precio_actual:
        upside_puntos = 5.0
        upside_detalle = "Consenso insuficiente — puntaje neutro"
    else:
        upside = (precio_consenso - precio_actual) / precio_actual
        if upside >= 0.30:
            upside_puntos = 10.0
        elif upside >= 0.15:
            upside_puntos = 7.5
        elif upside >= 0.00:
            upside_puntos = 5.0
        elif upside >= -0.15:
            upside_puntos = 2.5
        else:
            upside_puntos = 0.0
        upside_detalle = f"Upside {upside * 100:+.1f}%"

    dr = _sf(consenso.get("disagreement_ratio"))
    if dr is None:
        confianza_puntos = 5.0
        confianza_detalle = "DR no disponible — puntaje neutro"
    elif dr < 0.10:
        confianza_puntos = 10.0
        confianza_detalle = f"DR {dr * 100:.1f}% — alta consistencia"
    elif dr < 0.20:
        confianza_puntos = 7.0
        confianza_detalle = f"DR {dr * 100:.1f}% — consistencia moderada"
    elif dr <= 0.35:
        confianza_puntos = 4.0
        confianza_detalle = f"DR {dr * 100:.1f}% — dispersión elevada"
    else:
        confianza_puntos = 1.0
        confianza_detalle = f"DR {dr * 100:.1f}% — alta dispersión"

    z_score = _sf(altman.get("z_score"))
    altman_disponible = bool(altman.get("disponible")) and z_score is not None
    if not altman_disponible:
        solvencia_puntos = 5.0
        solvencia_detalle = "Z-Score no disponible — puntaje neutro"
    elif z_score > 2.99:
        solvencia_puntos = 10.0
        solvencia_detalle = f"Z-Score {z_score:.2f} — zona segura"
    elif z_score >= 1.81:
        solvencia_puntos = 6.0
        solvencia_detalle = f"Z-Score {z_score:.2f} — zona gris"
    else:
        solvencia_puntos = 2.0
        solvencia_detalle = f"Z-Score {z_score:.2f} — zona peligro"

    filtros = []
    if isinstance(filtros_dict, dict):
        filtros = list(filtros_dict.values())
    elif isinstance(filtros_dict, (list, tuple)):
        filtros = list(filtros_dict)

    filtros_totales = len(filtros)
    if not filtros_totales:
        fundamentals_puntos = 5.0
        fundamentals_detalle = "Filtros no disponibles — puntaje neutro"
    else:
        filtros_ok = 0
        for filtro in filtros:
            cumple = filtro.get("cumple") if isinstance(filtro, dict) else getattr(filtro, "cumple", False)
            if cumple:
                filtros_ok += 1
        ratio_cumplimiento = filtros_ok / filtros_totales
        if ratio_cumplimiento >= 0.75:
            fundamentals_puntos = 10.0
        elif ratio_cumplimiento >= 0.50:
            fundamentals_puntos = 6.0
        elif ratio_cumplimiento >= 0.25:
            fundamentals_puntos = 3.0
        else:
            fundamentals_puntos = 1.0
        fundamentals_detalle = f"{filtros_ok} de {filtros_totales} filtros OK"

    componentes = {
        "upside": {
            "puntos": upside_puntos,
            "peso": 0.40,
            "detalle": upside_detalle,
        },
        "confianza": {
            "puntos": confianza_puntos,
            "peso": 0.20,
            "detalle": confianza_detalle,
        },
        "solvencia": {
            "puntos": solvencia_puntos,
            "peso": 0.20,
            "detalle": solvencia_detalle,
        },
        "fundamentals": {
            "puntos": fundamentals_puntos,
            "peso": 0.20,
            "detalle": fundamentals_detalle,
        },
    }

    raw = (
        upside_puntos * 0.40
        + confianza_puntos * 0.20
        + solvencia_puntos * 0.20
        + fundamentals_puntos * 0.20
    )
    score = round(max(0.0, min(10.0, raw)), 1)

    nota_etapa = None
    if stage_num in (1, 2):
        nota_etapa = "Score orientativo — en etapas tempranas la incertidumbre es muy alta"
    elif stage_num == 6 and altman_disponible and z_score < 1.81:
        score = min(score, 4.0)
        nota_etapa = "Empresa en declive con riesgo de insolvencia — score limitado"

    if score >= 6.5:
        recomendacion = "Comprar"
    elif score >= 3.5:
        recomendacion = "Mantener"
    else:
        recomendacion = "Vender"

    return {
        "score": score,
        "recomendacion": recomendacion,
        "componentes": componentes,
        "nota_etapa": nota_etapa,
        "advertencia": "Score orientativo. No constituye asesoramiento financiero.",
    }
