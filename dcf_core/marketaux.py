"""Utilities to fetch company news from the Marketaux API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import requests


class MarketauxError(RuntimeError):
    """Raised when Marketaux cannot fulfill a request."""


@dataclass(frozen=True)
class MarketauxNewsItem:
    """Represents a single Marketaux news article."""

    title: str
    source: Optional[str]
    summary: Optional[str]
    url: str
    image: Optional[str]
    published_at: Optional[datetime]


def _get_api_key() -> str:
    key = os.environ.get("MARKETAUX_API_KEY", "").strip()
    if not key:
        raise MarketauxError(
            "No se encontró la clave de API de Marketaux. Definí MARKETAUX_API_KEY para habilitar las noticias."
        )
    return key


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None

    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def obtener_noticias_marketaux(ticker: str, limite: int = 6) -> List[MarketauxNewsItem]:
    """Fetches recent Marketaux news for the provided ticker symbol."""

    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise MarketauxError("El ticker proporcionado no es válido.")

    api_key = _get_api_key()
    lim = max(1, min(int(limite or 6), 50))

    params = {
        "symbols": ticker,
        "filter_entities": "true",
        "sort": "published_at:desc",
        "limit": str(lim),
        "language": "en,es",
        "api_token": api_key,
    }

    url = "https://api.marketaux.com/v1/news/all"
    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:  # pragma: no cover - dependiente de la red
        raise MarketauxError(f"No se pudieron obtener noticias de Marketaux ({exc}).") from exc

    if response.status_code == 401:
        raise MarketauxError("Marketaux devolvió 401 (token inválido o expirado). Verificá MARKETAUX_API_KEY.")

    if response.status_code == 429:
        raise MarketauxError("Marketaux devolvió 429 (límite de peticiones superado). Intenta nuevamente más tarde.")

    if response.status_code != 200:
        snippet = response.text.strip().replace("\n", " ")[:200]
        raise MarketauxError(f"Marketaux devolvió un error inesperado ({response.status_code}: {snippet}).")

    try:
        payload = response.json()
    except ValueError as exc:  # pragma: no cover - depends on provider
        raise MarketauxError("Marketaux devolvió un cuerpo no válido al solicitar noticias.") from exc

    data = payload.get("data")
    if not isinstance(data, list):
        raise MarketauxError("Marketaux devolvió un formato inesperado para las noticias.")

    items: List[MarketauxNewsItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue

        title = (entry.get("title") or "").strip()
        url_art = (entry.get("url") or entry.get("article_url") or "").strip()
        if not title or not url_art:
            continue

        fuente_raw = entry.get("source")
        if isinstance(fuente_raw, dict):
            source = (fuente_raw.get("title") or fuente_raw.get("name") or fuente_raw.get("domain") or "").strip() or None
        else:
            source = (str(fuente_raw or "").strip() or None)

        resumen = (
            entry.get("description")
            or entry.get("summary")
            or entry.get("snippet")
            or entry.get("content")
        )
        summary = str(resumen).strip() if resumen else None

        imagen = (
            entry.get("image_url")
            or entry.get("image_url_small")
            or entry.get("image")
        )
        image = str(imagen).strip() if imagen else None

        published = _parse_datetime(entry.get("published_at") or entry.get("created_at"))

        items.append(
            MarketauxNewsItem(
                title=title,
                source=source,
                summary=summary,
                url=url_art,
                image=image,
                published_at=published,
            )
        )

    items.sort(key=lambda n: (n.published_at is None, n.published_at and -n.published_at.timestamp()))
    if lim and len(items) > lim:
        items = items[:lim]
    return items

