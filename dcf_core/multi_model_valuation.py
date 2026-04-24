"""
Motor de valuación multi-modelo.

Calcula el valor intrínseco de una empresa usando 9 modelos distintos,
los pondera según la etapa del ciclo de vida detectada por company_stage.py,
y produce un precio consenso final.

No realiza llamadas adicionales a APIs — usa exclusivamente los datos
ya presentes en el dict `financials` (resultado de analizar_empresa()).
"""

from __future__ import annotations

import math
from typing import Optional

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
        "fwd_earnings": 0.0, "fwd_fcf": 0.0,
        "tam": 1.0, "liquidation_value": 0.0,
        "tam_note": True, "asset_note": False,
    },
    2: {  # Hyper Growth
        "dcf": 0.0, "reverse_dcf": 0.0,
        "pe_trailing": 0.0, "ps": 1.0,
        "pgp": 1.0, "pfcf_trailing": 0.0,
        "fwd_earnings": 0.0, "fwd_fcf": 0.0,
        "tam": 1.0, "liquidation_value": 0.0,
        "tam_note": True, "asset_note": False,
    },
    3: {  # Break Even
        "dcf": 0.5, "reverse_dcf": 0.5,
        "pe_trailing": 0.0, "ps": 1.0,
        "pgp": 1.0, "pfcf_trailing": 0.0,
        "fwd_earnings": 0.5, "fwd_fcf": 0.5,
        "tam": 0.5, "liquidation_value": 0.0,
        "tam_note": False, "asset_note": False,
    },
    4: {  # Operating Leverage
        "dcf": 0.5, "reverse_dcf": 0.5,
        "pe_trailing": 0.5, "ps": 1.0,
        "pgp": 1.0, "pfcf_trailing": 0.5,
        "fwd_earnings": 1.0, "fwd_fcf": 1.0,
        "tam": 0.5, "liquidation_value": 0.0,
        "tam_note": False, "asset_note": False,
    },
    5: {  # Capital Return
        "dcf": 1.0, "reverse_dcf": 1.0,
        "pe_trailing": 1.0, "ps": 0.5,
        "pgp": 0.5, "pfcf_trailing": 1.0,
        "fwd_earnings": 1.0, "fwd_fcf": 1.0,
        "tam": 0.0, "liquidation_value": 0.0,
        "tam_note": False, "asset_note": False,
    },
    6: {  # Decline — los modelos de crecimiento pierden relevancia
        "dcf": 0.0, "reverse_dcf": 0.0,
        "pe_trailing": 0.0, "ps": 0.0,
        "pgp": 0.0, "pfcf_trailing": 0.0,
        "fwd_earnings": 0.0, "fwd_fcf": 0.0,
        "tam": 0.0, "liquidation_value": 0.70,
        "tam_note": False, "asset_note": True,
    },
}

_MODEL_KEYS = ["dcf", "reverse_dcf", "pe_trailing", "ps", "pgp",
               "tam", "pfcf_trailing", "fwd_earnings", "fwd_fcf", "liquidation_value"]

_MODEL_NOMBRES = {
    "dcf":               "DCF",
    "reverse_dcf":       "Reverse DCF",
    "pe_trailing":       "P/E Trailing",
    "ps":                "Price to Sales",
    "pgp":               "Price to Gross Profit",
    "tam":               "TAM asistido",
    "pfcf_trailing":     "P/FCF Trailing",
    "fwd_earnings":      "P/E Forward",
    "fwd_fcf":           "P/FCF Forward",
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

def _modelo_dcf(financials: dict) -> dict:
    """Modelo 1 — Reutiliza el DCF ya calculado por la app."""
    valor = _sf(financials.get("valor_intrinseco"))
    crecimiento_pct = _sf((financials.get("metricas") or {}).get("crecimiento_pct"))
    wacc_pct = _sf((financials.get("metricas") or {}).get("wacc_pct"))

    detalle = "Proyección DCF ya calculada"
    if crecimiento_pct is not None and wacc_pct is not None:
        detalle = (
            f"Proyección 5 años con crecimiento {crecimiento_pct:.1f}% "
            f"y WACC {wacc_pct:.2f}%"
        )

    return {
        "valor": valor,
        "aplicable": valor is not None,
        "detalle": detalle,
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
    g_terminal = 0.025

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

    if gp is None or acciones is None or not acciones:
        return {"valor": None, "aplicable": False,
                "detalle": "Gross Profit TTM o acciones no disponibles"}

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
        "aplicable": True,
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
    net_margin = _sf(financials.get("net_margin")) or 0.10
    rev_growth = _sf((financials.get("metricas") or {}).get("crecimiento_cagr")) or 0.05

    if fcf_ttm is None or acciones is None or not acciones or fcf_ttm <= 0:
        return {"valor": None, "aplicable": False,
                "detalle": "FCF TTM negativo o no disponible — Forward P/FCF no aplicable"}

    # FCF forward estimado con ajuste de margen
    margen_fcf = abs(net_margin)
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
    Ejecuta los 9 modelos de valuación y calcula el precio consenso ponderado.

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
        "fwd_earnings":      _modelo_fwd_earnings(financials, ratios),
        "fwd_fcf":           _modelo_fwd_fcf(financials, ratios),
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

        if key == "reverse_dcf":
            entry["g_implicita"] = r.get("g_implicita")
            entry["g_implicita_pct"] = r.get("g_implicita_pct")
            entry["cagr_historico_pct"] = r.get("cagr_historico_pct")
            entry["veredicto"] = r.get("veredicto")
        elif key == "tam":
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

        consenso = {
            "precio": precio_consenso,
            "precio_actual": precio_actual,
            "upside_pct": upside_pct,
            "rango_min": valor_min,
            "rango_max": valor_max,
            "veredicto": veredicto_final,
            "modelos_usados": len(modelos_usados),
            "modelos_usados_keys": modelos_usados,
            "modelos_excluidos": modelos_excluidos,
            "confianza": _confianza(len(modelos_usados)),
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

    return {
        "ticker": ticker,
        "modelos": modelos,
        "consenso": consenso,
        "stage_context": stage_context,
        "altman": altman,
    }
