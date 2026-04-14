"""
Motor de valuación multi-modelo.

Calcula el valor intrínseco de una empresa usando 8 modelos distintos,
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


# ---------------------------------------------------------------------------
# Pesos por etapa del ciclo de vida
# ---------------------------------------------------------------------------

WEIGHTS: dict[int, dict[str, float | bool]] = {
    1: {  # Startup
        "dcf": 0.00, "reverse_dcf": 0.00,
        "pe_trailing": 0.00, "ps": 0.40,
        "pgp": 0.40, "pfcf_trailing": 0.00,
        "fwd_earnings": 0.00, "fwd_fcf": 0.00,
        "tam_note": True, "asset_note": False,
    },
    2: {  # Hyper Growth
        "dcf": 0.00, "reverse_dcf": 0.00,
        "pe_trailing": 0.00, "ps": 0.40,
        "pgp": 0.35, "pfcf_trailing": 0.00,
        "fwd_earnings": 0.00, "fwd_fcf": 0.25,
        "tam_note": True, "asset_note": False,
    },
    3: {  # Break Even
        "dcf": 0.15, "reverse_dcf": 0.10,
        "pe_trailing": 0.00, "ps": 0.25,
        "pgp": 0.20, "pfcf_trailing": 0.00,
        "fwd_earnings": 0.15, "fwd_fcf": 0.15,
        "tam_note": False, "asset_note": False,
    },
    4: {  # Operating Leverage
        "dcf": 0.20, "reverse_dcf": 0.10,
        "pe_trailing": 0.15, "ps": 0.10,
        "pgp": 0.10, "pfcf_trailing": 0.15,
        "fwd_earnings": 0.10, "fwd_fcf": 0.10,
        "tam_note": False, "asset_note": False,
    },
    5: {  # Capital Return
        "dcf": 0.30, "reverse_dcf": 0.15,
        "pe_trailing": 0.20, "ps": 0.00,
        "pgp": 0.00, "pfcf_trailing": 0.20,
        "fwd_earnings": 0.10, "fwd_fcf": 0.05,
        "tam_note": False, "asset_note": False,
    },
    6: {  # Decline — todos en cero; se muestra aviso especial
        "dcf": 0.00, "reverse_dcf": 0.00,
        "pe_trailing": 0.00, "ps": 0.00,
        "pgp": 0.00, "pfcf_trailing": 0.00,
        "fwd_earnings": 0.00, "fwd_fcf": 0.00,
        "tam_note": False, "asset_note": True,
    },
}

_MODEL_KEYS = ["dcf", "reverse_dcf", "pe_trailing", "ps", "pgp",
               "pfcf_trailing", "fwd_earnings", "fwd_fcf"]

_MODEL_NOMBRES = {
    "dcf":           "DCF",
    "reverse_dcf":   "Reverse DCF",
    "pe_trailing":   "P/E Trailing",
    "ps":            "Price to Sales",
    "pgp":           "Price to Gross Profit",
    "pfcf_trailing": "P/FCF Trailing",
    "fwd_earnings":  "P/E Forward",
    "fwd_fcf":       "P/FCF Forward",
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


def _confianza(n_modelos: int) -> str:
    """Nivel de confianza según cantidad de modelos usados."""
    if n_modelos >= 5:
        return "Alta"
    if n_modelos >= 3:
        return "Media"
    return "Baja"


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

    enterprise_value = precio * acciones + deuda
    g_terminal = 0.025

    def _ev_dado_g(g: float) -> float:
        """Calcula enterprise value teórico para una tasa g."""
        if wacc <= g:
            return float("inf")
        fcf_proj = [fcf_ttm * (1 + g) ** t for t in range(1, 6)]
        pv_fcf = sum(f / (1 + wacc) ** t for t, f in enumerate(fcf_proj, 1))
        if wacc <= g_terminal:
            return pv_fcf
        vt = fcf_proj[-1] * (1 + g_terminal) / (wacc - g_terminal)
        pv_vt = vt / (1 + wacc) ** 5
        return pv_fcf + pv_vt

    def _objetivo(g: float) -> float:
        return _ev_dado_g(g) - enterprise_value

    try:
        g_impl = _brentq(_objetivo, -0.50, wacc - 0.005, xtol=1e-6, maxiter=200)
    except (ValueError, RuntimeError):
        return {"valor": None, "g_implicita": None, "veredicto": None,
                "aplicable": False,
                "detalle": "No se pudo converger la solución numérica"}

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
        f"CAGR histórico: {cagr*100:.1f}%. Valuación implícita: {veredicto}."
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


def _modelo_pfcf_trailing(financials: dict, ratios: dict) -> dict:
    """Modelo 6 — Price to FCF Trailing."""
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
    """Modelo 7 — Price to Forward Earnings."""
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
    """Modelo 8 — Price to Forward FCF."""
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
    Ejecuta los 8 modelos de valuación y calcula el precio consenso ponderado.

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
        "dcf":           _modelo_dcf(financials),
        "reverse_dcf":   _modelo_reverse_dcf(financials, wacc),
        "pe_trailing":   _modelo_pe_trailing(financials, ratios),
        "ps":            _modelo_ps(financials, ratios),
        "pgp":           _modelo_pgp(financials, ratios),
        "pfcf_trailing": _modelo_pfcf_trailing(financials, ratios),
        "fwd_earnings":  _modelo_fwd_earnings(financials, ratios),
        "fwd_fcf":       _modelo_fwd_fcf(financials, ratios),
    }

    # ── Pesos ajustados ───────────────────────────────────────────────────────
    pesos_ajustados = _redistribuir_pesos(raw_weights, resultados_raw)

    # ── Construir dict de modelos con metadatos completos ────────────────────
    modelos: dict[str, dict] = {}
    for key in _MODEL_KEYS:
        r = resultados_raw[key]
        peso_raw = float(raw_weights.get(key, 0.0)) if isinstance(raw_weights.get(key), (int, float)) else 0.0
        peso_final = pesos_ajustados.get(key, 0.0)

        if peso_raw == 0.0:
            relevancia = "No útil"
        elif key in ("dcf", "reverse_dcf", "pe_trailing", "pfcf_trailing", "fwd_earnings", "fwd_fcf"):
            relevancia = "Útil" if peso_raw >= 0.15 else "Algo útil"
        else:
            relevancia = "Útil" if peso_raw >= 0.20 else "Algo útil"

        entry: dict = {
            "nombre": _MODEL_NOMBRES[key],
            "valor": r.get("valor"),
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
        }
    else:
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
        }

    # ── Contexto de etapa ─────────────────────────────────────────────────────
    from .company_stage import STAGE_META  # import diferido para evitar circular
    meta = STAGE_META.get(stage, STAGE_META[4])

    nota_especial: Optional[str] = None
    if raw_weights.get("tam_note"):
        nota_especial = (
            "En etapas tempranas el precio consenso tiene alta incertidumbre. "
            "El TAM y la trayectoria de crecimiento son más relevantes que "
            "cualquier modelo cuantitativo."
        )
    elif raw_weights.get("asset_note"):
        nota_especial = (
            "En etapa de declive los modelos de flujo de caja tienen utilidad "
            "limitada. Considerá el valor de liquidación y los dividendos."
        )

    utiles_keys = [k for k in _MODEL_KEYS if float(raw_weights.get(k, 0) or 0) > 0]
    no_utiles_keys = [k for k in _MODEL_KEYS if float(raw_weights.get(k, 0) or 0) == 0]

    stage_context = {
        "stage": stage,
        "stage_name": meta["nombre"],
        "modelos_utiles": [_MODEL_NOMBRES[k] for k in utiles_keys],
        "modelos_no_utiles": [_MODEL_NOMBRES[k] for k in no_utiles_keys],
        "nota_especial": nota_especial,
    }

    return {
        "ticker": ticker,
        "modelos": modelos,
        "consenso": consenso,
        "stage_context": stage_context,
    }
