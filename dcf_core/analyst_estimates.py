"""Estimaciones de analistas: precio objetivo y recomendaciones de consenso.

Consulta Finnhub como fuente primaria y FMP como respaldo. Diseñado para
importarse fuera de Django: no usa settings, ORM ni cache framework.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests


_FINNHUB_PRICE_TARGET_URL = "https://finnhub.io/api/v1/stock/price-target"
_FINNHUB_RECOMMENDATIONS_URL = "https://finnhub.io/api/v1/stock/recommendation"
_FMP_PRICE_TARGET_URL = "https://financialmodelingprep.com/stable/price-target-consensus"
_FMP_RECOMMENDATIONS_URL = "https://financialmodelingprep.com/stable/grades-consensus"

_REQUEST_TIMEOUT = 3
_YFINANCE_TIMEOUT = 5
_CACHE_TTL_SECONDS = 240 * 60
_CACHE_SCHEMA_VERSION = "analyst-v3"

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_ADVERTENCIA = (
    "Las estimaciones de analistas pueden estar sesgadas hacia el optimismo. "
    "Los analistas que cubren una empresa suelen tener relaciones comerciales con ella."
)


def get_analyst_estimates(ticker: str, precio_actual: float | None = None) -> dict:
    """Devuelve estimaciones de analistas (precio objetivo y recomendaciones) para un ticker.

    Intenta Finnhub como fuente primaria y FMP como respaldo. Si ambas APIs
    fallan o no retornan datos, devuelve disponible=False sin lanzar excepciones.
    El parámetro precio_actual se usa para calcular el upside/downside.
    """

    symbol = (ticker or "").strip().upper()
    if not symbol:
        return _sin_datos(symbol)

    cache_key = f"{_CACHE_SCHEMA_VERSION}:{symbol}"
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL_SECONDS:
        result = cached[1]
        if precio_actual is not None and result.get("disponible"):
            return _recalcular_upside(result, precio_actual)
        return result

    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    fmp_key = os.environ.get("FMP_API_KEY", "")

    precio_objetivo = _obtener_precio_objetivo(symbol, finnhub_key, fmp_key)
    recomendaciones = _obtener_recomendaciones(symbol, finnhub_key, fmp_key)

    if not precio_objetivo and not recomendaciones:
        payload = _sin_datos(symbol)
        _CACHE[cache_key] = (time.time(), payload)
        return payload

    payload = _construir_payload(symbol, precio_objetivo, recomendaciones, precio_actual)
    _CACHE[cache_key] = (time.time(), payload)
    return payload


# ── Orquestación por fuente ────────────────────────────────────────────────────

def _obtener_precio_objetivo(symbol: str, finnhub_key: str, fmp_key: str) -> dict | None:
    """Obtiene precio objetivo: Finnhub → FMP → yfinance."""

    if finnhub_key:
        data = _fetch_finnhub_price_target(symbol, finnhub_key)
        if data:
            return data
    if fmp_key:
        data = _fetch_fmp_price_target(symbol, fmp_key)
        if data:
            return data
    return _fetch_yfinance_price_target(symbol)


def _obtener_recomendaciones(symbol: str, finnhub_key: str, fmp_key: str) -> dict | None:
    """Obtiene recomendaciones de analistas: primero Finnhub, luego FMP."""

    if finnhub_key:
        data = _fetch_finnhub_recommendations(symbol, finnhub_key)
        if data:
            return data
    if fmp_key:
        data = _fetch_fmp_recommendations(symbol, fmp_key)
        if data:
            return data
    return None


# ── Fetchers Finnhub ───────────────────────────────────────────────────────────

def _fetch_finnhub_price_target(symbol: str, api_key: str) -> dict | None:
    """Llama al endpoint de precio objetivo de Finnhub."""

    try:
        resp = requests.get(
            _FINNHUB_PRICE_TARGET_URL,
            params={"symbol": symbol, "token": api_key},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    medio = _to_float(data.get("targetMean"))
    if medio is None:
        return None

    return {
        "medio": medio,
        "mediana": _to_float(data.get("targetMedian")),
        "alto": _to_float(data.get("targetHigh")),
        "bajo": _to_float(data.get("targetLow")),
        "num_analistas": _to_int(data.get("numberOfAnalysts")),
        "ultima_actualizacion": _normalizar_fecha(data.get("lastUpdated")),
        "fuente": "finnhub",
    }


def _fetch_finnhub_recommendations(symbol: str, api_key: str) -> dict | None:
    """Llama al endpoint de recomendaciones de Finnhub y usa el período más reciente."""

    try:
        resp = requests.get(
            _FINNHUB_RECOMMENDATIONS_URL,
            params={"symbol": symbol, "token": api_key},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None

    try:
        item = sorted(data, key=lambda x: x.get("period", ""), reverse=True)[0]
    except Exception:
        item = data[0]

    return _normalizar_recomendaciones_item(item, fuente="finnhub")


# ── Fetchers FMP ───────────────────────────────────────────────────────────────

def _fetch_fmp_price_target(symbol: str, api_key: str) -> dict | None:
    """Llama al endpoint de precio objetivo de FMP."""

    try:
        resp = requests.get(
            _FMP_PRICE_TARGET_URL,
            params={"symbol": symbol, "apikey": api_key},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if isinstance(data, list):
        if not data:
            return None
        data = data[0]

    if not isinstance(data, dict):
        return None

    medio = _to_float(data.get("targetConsensus") or data.get("targetMean"))
    if medio is None:
        return None

    return {
        "medio": medio,
        "mediana": _to_float(data.get("targetMedian")),
        "alto": _to_float(data.get("targetHigh")),
        "bajo": _to_float(data.get("targetLow")),
        "num_analistas": _to_int(data.get("numberOfAnalysts")),
        "ultima_actualizacion": _normalizar_fecha(data.get("lastUpdated")),
        "fuente": "fmp",
    }


def _fetch_fmp_recommendations(symbol: str, api_key: str) -> dict | None:
    """Llama al endpoint de recomendaciones de FMP."""

    try:
        resp = requests.get(
            _FMP_RECOMMENDATIONS_URL,
            params={"symbol": symbol, "apikey": api_key},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    if isinstance(data, list):
        if not data:
            return None
        data = data[0]

    if not isinstance(data, dict):
        return None

    return _normalizar_recomendaciones_item(data, fuente="fmp")


# ── Fetcher yfinance (fallback universal para precio objetivo) ─────────────────

def _fetch_yfinance_price_target(symbol: str) -> dict | None:
    """Obtiene precio objetivo desde yfinance como fallback sin credenciales.

    Ejecuta la llamada en un hilo separado con timeout para no bloquear el hilo
    principal si yfinance tarda más de lo esperado.
    """

    def _fetch() -> dict:
        import yfinance as yf  # importación diferida para no penalizar módulos que no lo usan
        return yf.Ticker(symbol).info

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            info = executor.submit(_fetch).result(timeout=_YFINANCE_TIMEOUT)
    except Exception:
        return None

    if not isinstance(info, dict):
        return None

    medio = _to_float(info.get("targetMeanPrice"))
    if medio is None:
        return None

    return {
        "medio": medio,
        "mediana": _to_float(info.get("targetMedianPrice")),
        "alto": _to_float(info.get("targetHighPrice")),
        "bajo": _to_float(info.get("targetLowPrice")),
        "num_analistas": _to_int(info.get("numberOfAnalystOpinions")),
        "ultima_actualizacion": None,
        "fuente": "yfinance",
    }


# ── Normalización y cálculo ────────────────────────────────────────────────────

def _normalizar_recomendaciones_item(item: dict, fuente: str) -> dict | None:
    """Extrae y valida los conteos de recomendaciones de un item de API."""

    strong_buy = _to_int(item.get("strongBuy") or item.get("strong_buy")) or 0
    buy = _to_int(item.get("buy")) or 0
    hold = _to_int(item.get("hold") or item.get("neutral")) or 0
    sell = _to_int(item.get("sell")) or 0
    strong_sell = _to_int(item.get("strongSell") or item.get("strong_sell")) or 0

    total = strong_buy + buy + hold + sell + strong_sell
    if total == 0:
        return None

    return {
        "strong_buy": strong_buy,
        "buy": buy,
        "hold": hold,
        "sell": sell,
        "strong_sell": strong_sell,
        "total": total,
        "fuente": fuente,
    }


def _calcular_consenso(recomendaciones: dict) -> tuple[str, str]:
    """Devuelve (etiqueta_consenso, color) según la distribución de recomendaciones."""

    total = recomendaciones.get("total", 0)
    if total == 0:
        return "Mantener", "gris"

    pct_compra = (recomendaciones.get("strong_buy", 0) + recomendaciones.get("buy", 0)) / total
    pct_venta = (recomendaciones.get("sell", 0) + recomendaciones.get("strong_sell", 0)) / total

    if pct_compra > 0.60:
        return "Comprar", "verde"
    if pct_venta > 0.40:
        return "Vender", "rojo"
    return "Mantener", "gris"


def _calcular_upside(precio_objetivo_medio: float, precio_actual: float | None) -> float | None:
    """Calcula el upside porcentual respecto al precio actual."""

    if precio_actual is None or precio_actual <= 0:
        return None
    return round((precio_objetivo_medio - precio_actual) / precio_actual * 100, 1)


def _calcular_porcentajes(recomendaciones: dict) -> dict:
    """Agrega campos pct_* al diccionario de recomendaciones para renderizar la barra."""

    total = recomendaciones.get("total", 0) or 1
    return {
        **recomendaciones,
        "pct_strong_buy": round(recomendaciones.get("strong_buy", 0) / total * 100, 1),
        "pct_buy": round(recomendaciones.get("buy", 0) / total * 100, 1),
        "pct_hold": round(recomendaciones.get("hold", 0) / total * 100, 1),
        "pct_sell": round(recomendaciones.get("sell", 0) / total * 100, 1),
        "pct_strong_sell": round(recomendaciones.get("strong_sell", 0) / total * 100, 1),
    }


def _calcular_posiciones_barra(
    bajo: float | None,
    alto: float | None,
    precio_actual: float | None,
    precio_medio: float | None,
) -> tuple[float | None, float | None]:
    """Devuelve (precio_actual_pct, medio_pct) como posición en el rango [bajo, alto] (0-100)."""

    if bajo is None or alto is None or alto <= bajo:
        return None, None
    rango = alto - bajo
    actual_pct = round(max(0.0, min(100.0, (precio_actual - bajo) / rango * 100)), 1) if precio_actual is not None else None
    medio_pct = round(max(0.0, min(100.0, (precio_medio - bajo) / rango * 100)), 1) if precio_medio is not None else None
    return actual_pct, medio_pct


def _construir_payload(
    symbol: str,
    precio_objetivo: dict | None,
    recomendaciones: dict | None,
    precio_actual: float | None,
) -> dict:
    """Arma el payload final con todos los campos normalizados."""

    fuente = (precio_objetivo or {}).get("fuente") or (recomendaciones or {}).get("fuente") or "desconocida"

    precio_medio = (precio_objetivo or {}).get("medio")
    bajo = (precio_objetivo or {}).get("bajo")
    alto = (precio_objetivo or {}).get("alto")
    upside_pct = _calcular_upside(precio_medio, precio_actual) if precio_medio is not None else None
    precio_actual_pct, medio_pct = _calcular_posiciones_barra(bajo, alto, precio_actual, precio_medio)

    consenso, color = _calcular_consenso(recomendaciones) if recomendaciones else ("Mantener", "gris")

    detalle_raw = recomendaciones or {"strong_buy": 0, "buy": 0, "hold": 0, "sell": 0, "strong_sell": 0, "total": 0, "fuente": ""}
    detalle = _calcular_porcentajes(detalle_raw)

    return {
        "ticker": symbol,
        "precio_actual": precio_actual,
        "precio_objetivo": {
            "medio": precio_medio,
            "mediana": (precio_objetivo or {}).get("mediana"),
            "alto": alto,
            "bajo": bajo,
            "upside_pct": upside_pct,
            "num_analistas": (precio_objetivo or {}).get("num_analistas"),
            "precio_actual_pct": precio_actual_pct,
            "medio_pct": medio_pct,
        },
        "recomendacion_consenso": consenso,
        "recomendacion_color": color,
        "recomendaciones_detalle": detalle,
        "ultima_actualizacion": (precio_objetivo or {}).get("ultima_actualizacion"),
        "fuente": fuente,
        "advertencia": _ADVERTENCIA,
        "disponible": True,
    }


def _recalcular_upside(result: dict, precio_actual: float) -> dict:
    """Retorna una copia del payload con el upside y posiciones de barra recalculados."""

    precio_objetivo = dict(result.get("precio_objetivo") or {})
    medio = precio_objetivo.get("medio")
    bajo = precio_objetivo.get("bajo")
    alto = precio_objetivo.get("alto")
    if medio is not None:
        precio_objetivo["upside_pct"] = _calcular_upside(medio, precio_actual)
    precio_actual_pct, medio_pct = _calcular_posiciones_barra(bajo, alto, precio_actual, medio)
    precio_objetivo["precio_actual_pct"] = precio_actual_pct
    precio_objetivo["medio_pct"] = medio_pct
    return {**result, "precio_objetivo": precio_objetivo, "precio_actual": precio_actual}


def _sin_datos(ticker: str) -> dict:
    """Payload vacío cuando no hay estimaciones disponibles."""

    return {
        "disponible": False,
        "ticker": ticker,
        "mensaje": "No se encontraron estimaciones de analistas para este ticker.",
    }


# ── Utilidades ─────────────────────────────────────────────────────────────────

def _to_float(value: Any) -> float | None:
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    result = _to_float(value)
    return int(result) if result is not None else None


def _normalizar_fecha(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    return value[:10]
