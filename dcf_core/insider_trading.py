"""Obtención y normalización de transacciones de insiders.

El módulo consulta primero Finnhub y usa Financial Modeling Prep como fallback.
Está diseñado para poder importarse fuera de Django: no usa settings, ORM ni
cache framework.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


_FINNHUB_URL = "https://finnhub.io/api/v1/stock/insider-transactions"
_FMP_URL = "https://financialmodelingprep.com/stable/insider-trading"
_REQUEST_TIMEOUT = 3
_CACHE_TTL_SECONDS = 60 * 60
_MAX_TRANSACTIONS = 20
_LOOKBACK_DAYS = 180
_SCORE_LOOKBACK_DAYS = 90
_ADVERTENCIA = (
    "Las compras de insiders son señal más confiable que las ventas, que pueden obedecer "
    "a razones personales (diversificación, impuestos, planes 10b5-1) no relacionadas "
    "con la visión sobre la empresa."
)

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def get_insider_trading(ticker: str) -> dict:
    """Devuelve actividad reciente de insiders para un ticker.

    Intenta Finnhub como fuente primaria y FMP como respaldo. Si las APIs no
    responden, faltan credenciales o no hay actividad reciente, devuelve un
    payload no disponible sin lanzar excepciones para no romper el análisis DCF.
    """

    symbol = (ticker or "").strip().upper()
    if not symbol:
        return _sin_datos(symbol)

    cached = _CACHE.get(symbol)
    if cached and time.time() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    for fuente, fetcher, normalizer in (
        ("finnhub", _fetch_finnhub, _normalizar_finnhub),
        ("fmp", _fetch_fmp, _normalizar_fmp),
    ):
        try:
            raw_items = fetcher(symbol)
            if not raw_items:
                continue
            transacciones = _procesar_transacciones(raw_items, normalizer)
            if not transacciones:
                continue
            payload = _con_datos(symbol, fuente, transacciones)
            _CACHE[symbol] = (time.time(), payload)
            return payload
        except Exception:
            continue

    payload = _sin_datos(symbol)
    _CACHE[symbol] = (time.time(), payload)
    return payload


def _fetch_finnhub(ticker: str) -> list[dict[str, Any]]:
    """Consulta transacciones de insiders en Finnhub."""

    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not api_key:
        return []

    response = requests.get(
        _FINNHUB_URL,
        params={"symbol": ticker, "token": api_key},
        timeout=_REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        return []

    data = response.json()
    if isinstance(data, dict):
        items = data.get("data") or data.get("transactions") or []
    else:
        items = data
    return [item for item in items if isinstance(item, dict)]


def _fetch_fmp(ticker: str) -> list[dict[str, Any]]:
    """Consulta transacciones Form 4 en Financial Modeling Prep."""

    api_key = os.environ.get("FMP_API_KEY", "").strip()
    if not api_key:
        return []

    response = requests.get(
        _FMP_URL,
        params={"symbol": ticker, "limit": _MAX_TRANSACTIONS, "apikey": api_key},
        timeout=_REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        return []

    data = response.json()
    if isinstance(data, dict):
        items = data.get("data") or data.get("transactions") or []
    else:
        items = data
    return [item for item in items if isinstance(item, dict)]


def _procesar_transacciones(raw_items: list[dict[str, Any]], normalizer) -> list[dict[str, Any]]:
    """Normaliza, filtra por 180 días y ordena transacciones recientes."""

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=_LOOKBACK_DAYS)
    transacciones = []

    for item in raw_items:
        tx = normalizer(item)
        fecha = _parse_fecha(tx.get("fecha"))
        if fecha is None or fecha < cutoff:
            continue
        tx["fecha"] = fecha.isoformat()
        tx["fecha_display"] = fecha.strftime("%d/%m/%Y")
        tx["tipo_label"] = _tipo_label(tx["tipo"])
        tx["valor_total_display"] = _format_money_short(tx.get("valor_total"))
        tx["shares_display"] = _format_number(tx.get("shares"))
        tx["precio_display"] = _format_price(tx.get("precio"))
        transacciones.append(tx)

    transacciones.sort(key=lambda tx: tx["fecha"], reverse=True)
    return transacciones[:_MAX_TRANSACTIONS]


def _normalizar_finnhub(item: dict[str, Any]) -> dict[str, Any]:
    """Adapta una transacción de Finnhub al formato interno."""

    shares = _to_number(
        item.get("share")
        or item.get("shares")
        or item.get("change")
        or item.get("transactionShares")
        or item.get("securitiesTransacted")
    )
    precio = _to_number(
        item.get("transactionPrice")
        or item.get("price")
        or item.get("transaction_price")
    )
    valor_total = _calcular_valor_total(item, shares, precio)
    cargo = _normalizar_cargo(
        item.get("title")
        or item.get("officerTitle")
        or item.get("relationship")
        or item.get("isDirector")
    )

    return {
        "fecha": item.get("transactionDate") or item.get("filingDate") or item.get("date"),
        "insider_nombre": _texto(item.get("name") or item.get("insiderName") or item.get("reportingName")),
        "insider_cargo": cargo,
        "tipo": _clasificar_tipo(item.get("transactionCode") or item.get("code") or item.get("transactionType")),
        "shares": abs(shares) if shares is not None else None,
        "precio": precio,
        "valor_total": valor_total,
        "shares_restantes": _to_number(
            item.get("shareOwnedFollowingTransaction")
            or item.get("sharesOwnedFollowingTransaction")
            or item.get("securitiesOwned")
        ),
    }


def _normalizar_fmp(item: dict[str, Any]) -> dict[str, Any]:
    """Adapta una transacción de FMP al formato interno."""

    shares = _to_number(
        item.get("securitiesTransacted")
        or item.get("transactionShares")
        or item.get("shares")
        or item.get("acquistionOrDisposition")
    )
    precio = _to_number(item.get("price") or item.get("transactionPrice"))
    valor_total = _calcular_valor_total(item, shares, precio)

    return {
        "fecha": item.get("transactionDate") or item.get("filingDate") or item.get("date"),
        "insider_nombre": _texto(item.get("reportingName") or item.get("name") or item.get("insiderName")),
        "insider_cargo": _normalizar_cargo(item.get("typeOfOwner") or item.get("officerTitle") or item.get("title")),
        "tipo": _clasificar_tipo(item.get("transactionType") or item.get("transactionCode") or item.get("code")),
        "shares": abs(shares) if shares is not None else None,
        "precio": precio,
        "valor_total": valor_total,
        "shares_restantes": _to_number(
            item.get("securitiesOwned")
            or item.get("sharesOwnedFollowingTransaction")
            or item.get("shareOwnedFollowingTransaction")
        ),
    }


def _con_datos(ticker: str, fuente: str, transacciones: list[dict[str, Any]]) -> dict[str, Any]:
    """Construye la respuesta final con resumen y score de sentimiento."""

    score = _calcular_score(transacciones)
    return {
        "ticker": ticker,
        "score_sentimiento": score["score_sentimiento"],
        "score_color": score["score_color"],
        "score_descripcion": score["score_descripcion"],
        "periodo_analisis": "90 días",
        "resumen": score["resumen"],
        "transacciones": transacciones,
        "fuente": fuente,
        "advertencia": _ADVERTENCIA,
        "disponible": True,
    }


def _sin_datos(ticker: str) -> dict[str, Any]:
    """Construye la respuesta usada cuando no hay actividad reciente disponible."""

    return {
        "ticker": ticker,
        "disponible": False,
        "mensaje": f"No se encontraron transacciones de insiders en los últimos 180 días para este ticker.",
        "advertencia": _ADVERTENCIA,
    }


def _calcular_score(transacciones: list[dict[str, Any]]) -> dict[str, Any]:
    """Calcula el score de sentimiento usando compras y ventas de los últimos 90 días."""

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=_SCORE_LOOKBACK_DAYS)
    recientes = [
        tx for tx in transacciones
        if (_parse_fecha(tx.get("fecha")) or cutoff) >= cutoff and tx.get("tipo") != "ejercicio"
    ]

    compras = [tx for tx in recientes if tx.get("tipo") == "compra"]
    ventas = [tx for tx in recientes if tx.get("tipo") == "venta"]
    valor_compras = sum(_safe_float(tx.get("valor_total")) for tx in compras)
    valor_ventas = sum(_safe_float(tx.get("valor_total")) for tx in ventas)
    total_valor = valor_compras + valor_ventas

    if not recientes or total_valor == 0:
        ratio = 0.0
        score = "neutral"
    else:
        ratio = valor_compras / total_valor
        if ratio >= 0.70:
            score = "alcista"
        elif ratio <= 0.40:
            score = "bajista"
        else:
            score = "neutral"

    meta = {
        "alcista": {
            "score_color": "verde",
            "score_descripcion": "Los insiders están comprando activamente — señal positiva",
        },
        "neutral": {
            "score_color": "gris",
            "score_descripcion": "Actividad mixta de insiders — sin señal clara",
        },
        "bajista": {
            "score_color": "naranja",
            "score_descripcion": (
                "Los insiders están vendiendo — puede indicar cautela, aunque las ventas pueden "
                "deberse a diversificación o planes 10b5-1 programados"
            ),
        },
    }[score]

    resumen = {
        "total_transacciones": len(recientes),
        "total_compras": len(compras),
        "total_ventas": len(ventas),
        "valor_compras_usd": valor_compras,
        "valor_ventas_usd": valor_ventas,
        "valor_compras_display": _format_money_short(valor_compras),
        "valor_ventas_display": _format_money_short(valor_ventas),
        "ratio_compras": ratio,
        "ratio_compras_pct": round(ratio * 100),
    }

    return {"score_sentimiento": score, **meta, "resumen": resumen}


def _clasificar_tipo(code: Any) -> str:
    """Clasifica códigos Form 4 en categorías simples."""

    value = str(code or "").strip().upper()
    if value == "P" or value.startswith("P-"):
        return "compra"
    if value == "S" or value.startswith("S-"):
        return "venta"
    if value in {"A", "M"} or value.startswith("A-") or value.startswith("M-"):
        return "ejercicio"
    if "PURCHASE" in value or "BUY" in value or "ACQUISITION" in value:
        return "compra"
    if "SALE" in value or "SELL" in value or "DISPOSITION" in value:
        return "venta"
    if "OPTION" in value or "EXERCISE" in value:
        return "ejercicio"
    return "otro"


def _normalizar_cargo(value: Any) -> str:
    """Normaliza el cargo del insider a etiquetas cortas en español neutro."""

    texto = _texto(value)
    lookup = texto.upper()
    if "CEO" in lookup or "CHIEF EXECUTIVE" in lookup:
        return "CEO"
    if "CFO" in lookup or "CHIEF FINANCIAL" in lookup:
        return "CFO"
    if "COO" in lookup or "CHIEF OPERATING" in lookup:
        return "COO"
    if "DIRECTOR" in lookup or "BOARD" in lookup:
        return "Director"
    if "PRESIDENT" in lookup:
        return "Presidente"
    if "SVP" in lookup or "VP" in lookup or "VICE PRESIDENT" in lookup:
        return "VP"
    return texto[:30] if texto else "N/D"


def _parse_fecha(value: Any):
    """Convierte fechas comunes de APIs financieras a date."""

    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for candidate in (text[:10], text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _calcular_valor_total(item: dict[str, Any], shares: float | None, precio: float | None) -> float:
    """Calcula el valor total si no viene informado por la API."""

    explicit = _to_number(
        item.get("value")
        or item.get("totalValue")
        or item.get("transactionValue")
        or item.get("valueTransacted")
    )
    if explicit is not None:
        return abs(explicit)
    if shares is None or precio is None:
        return 0.0
    return abs(shares * precio)


def _to_number(value: Any) -> float | None:
    """Convierte valores numéricos con tolerancia a strings vacíos o símbolos."""

    if value in (None, "", "N/D"):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float:
    """Convierte a float y retorna cero si el valor no es numérico."""

    number = _to_number(value)
    return number if number is not None else 0.0


def _texto(value: Any) -> str:
    """Limpia texto recibido desde proveedores externos."""

    if isinstance(value, bool):
        return "Director" if value else ""
    return str(value or "").strip()


def _tipo_label(tipo: str) -> str:
    """Etiqueta visible para el tipo de transacción."""

    return {
        "compra": "Compra",
        "venta": "Venta",
        "ejercicio": "Ejercicio",
        "otro": "Otro",
    }.get(tipo, "Otro")


def _format_money_short(value: Any) -> str:
    """Formatea dólares en miles, millones o billones para la interfaz."""

    amount = _safe_float(value)
    abs_amount = abs(amount)
    if abs_amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if abs_amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    return f"${amount / 1_000:.0f}k"


def _format_number(value: Any) -> str:
    """Formatea cantidades de acciones sin decimales innecesarios."""

    number = _to_number(value)
    if number is None:
        return "N/D"
    return f"{number:,.0f}"


def _format_price(value: Any) -> str:
    """Formatea precios unitarios en dólares."""

    number = _to_number(value)
    if number is None:
        return "N/D"
    return f"${number:,.2f}"
