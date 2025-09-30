"""Helpers to interact with the Finnhub API for company news."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests


class FinnhubError(RuntimeError):
    """Raised when the Finnhub client cannot fulfill a request."""


@dataclass(frozen=True)
class FinnhubNewsItem:
    """Represents a single news article returned by Finnhub."""

    title: str
    source: Optional[str]
    summary: Optional[str]
    url: str
    image: Optional[str]
    published_at: Optional[datetime]


def _get_api_key() -> str:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        raise FinnhubError(
            "No se encontró la clave de API de Finnhub. Definí FINNHUB_API_KEY para habilitar las noticias."
        )
    return key


def _parse_datetime(epoch_seconds: Optional[int]) -> Optional[datetime]:
    if not epoch_seconds:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def obtener_noticias_finnhub(ticker: str, limite: int = 6, lookback_dias: int = 45) -> List[FinnhubNewsItem]:
    """Recupera noticias de Finnhub para un ticker dado en un rango temporal reciente."""

    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise FinnhubError("El ticker proporcionado no es válido.")

    api_key = _get_api_key()
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(days=max(lookback_dias, 1))

    params = {
        "symbol": ticker,
        "from": desde.date().isoformat(),
        "to": ahora.date().isoformat(),
        "token": api_key,
    }

    url = "https://finnhub.io/api/v1/company-news"
    try:
        respuesta = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:  # pragma: no cover - dependiente de red
        raise FinnhubError(f"No se pudieron obtener noticias de Finnhub ({exc}).") from exc

    if respuesta.status_code == 429:
        raise FinnhubError("Finnhub devolvió 429 (rate limit excedido). Intenta nuevamente en unos minutos.")

    if respuesta.status_code == 401:
        raise FinnhubError("Finnhub devolvió 401 (token inválido o expirado). Verificá FINNHUB_API_KEY.")

    if respuesta.status_code != 200:
        raise FinnhubError(
            f"Finnhub devolvió un error inesperado ({respuesta.status_code}: {respuesta.text.strip()[:200]})."
        )

    try:
        data = respuesta.json()
    except ValueError as exc:  # pragma: no cover - depende del proveedor
        raise FinnhubError("Finnhub devolvió un cuerpo no válido al solicitar noticias.") from exc

    if not isinstance(data, list):
        raise FinnhubError("Finnhub devolvió un formato inesperado para las noticias.")

    elementos: List[FinnhubNewsItem] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        titulo = (item.get("headline") or item.get("title") or "").strip()
        enlace = (item.get("url") or "").strip()
        if not titulo or not enlace:
            continue

        fuente = (item.get("source") or item.get("publisher") or "").strip() or None
        resumen = (item.get("summary") or item.get("text") or "").strip() or None
        imagen = (item.get("image") or item.get("thumbnail") or "").strip() or None
        publicado = _parse_datetime(item.get("datetime") or item.get("publishedTime"))

        elementos.append(
            FinnhubNewsItem(
                title=titulo,
                source=fuente,
                summary=resumen,
                url=enlace,
                image=imagen,
                published_at=publicado,
            )
        )

    elementos.sort(key=lambda n: (n.published_at is None, n.published_at and -n.published_at.timestamp()))
    if limite > 0:
        elementos = elementos[:limite]
    return elementos
