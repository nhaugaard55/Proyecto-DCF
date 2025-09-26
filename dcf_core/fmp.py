"""Utilities to interact with the Financial Modeling Prep API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import requests


class FMPClientError(RuntimeError):
    """Raised when the Financial Modeling Prep client cannot fulfil a request."""


@dataclass(frozen=True)
class FCFEntry:
    """Represents a single historical free cash flow data point."""

    year: Optional[int]
    value: float


class FMPClient:
    """Very small helper around the Financial Modeling Prep REST API."""

    _BASE_URL = "https://financialmodelingprep.com"

    def __init__(self, api_key: Optional[str] = None, session: Optional[requests.Session] = None) -> None:
        self._api_key = api_key or os.environ.get("FMP_API_KEY")
        self._session = session or requests.Session()
        if not self._api_key:
            raise FMPClientError(
                "No se encontró la clave de API para Financial Modeling Prep. "
                "Definí la variable de entorno FMP_API_KEY antes de ejecutar el análisis."
            )

    def _request(self, endpoint: str, params: Optional[dict] = None):
        params = params.copy() if params else {}
        params["apikey"] = self._api_key
        url = f"{self._BASE_URL}/{endpoint}"
        try:
            response = self._session.get(url, params=params, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise FMPClientError(
                f"No se pudo obtener información de Financial Modeling Prep ({exc})."
            ) from exc

        return response.json()

    def get_cash_flow_statements(self, ticker: str, limit: int = 10) -> list:
        """Return raw annual cash-flow statements for the given ticker."""
        ticker = ticker.upper().strip()
        if not ticker:
            raise FMPClientError("El ticker proporcionado no es válido.")
        effective_limit = min(limit, 5)
        params = {"symbol": ticker, "period": "annual", "limit": effective_limit}
        data = self._request("stable/cash-flow-statement", params=params)

        if isinstance(data, dict):
            error_message = data.get("Error Message") or data.get("error")
            if error_message and "Legacy Endpoint" in str(error_message):
                data = self._request(
                    f"api/v3/cash-flow-statement/{ticker}",
                    params={"period": "annual", "limit": effective_limit}
                )
            else:
                raise FMPClientError(
                    f"Financial Modeling Prep devolvió un error al pedir el cash flow: {error_message or data}."
                )

        if not isinstance(data, list):
            raise FMPClientError(
                "Financial Modeling Prep devolvió un formato inesperado al pedir el cash flow."
            )

        return data

    def get_free_cash_flow_history(self, ticker: str, limit: int = 10) -> List[FCFEntry]:
        """Return a list of free cash flow entries (most recent first)."""
        statements = self.get_cash_flow_statements(ticker, limit=limit)
        history: List[FCFEntry] = []
        for statement in statements:
            raw_value = statement.get("freeCashFlow")
            if raw_value in (None, ""):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue

            year_value = statement.get("calendarYear") or ""
            year: Optional[int]
            try:
                year = int(year_value)
            except (TypeError, ValueError):
                # Algunos tickers devuelven "date" como AAAA-MM-DD
                raw_date = statement.get("date") or ""
                try:
                    year = int(str(raw_date)[:4]) if raw_date else None
                except (TypeError, ValueError):
                    year = None

            history.append(FCFEntry(year=year, value=value))

        return history


def obtener_fcf_historico(ticker: str, minimo: int = 6, limite: int = 10) -> List[FCFEntry]:
    """
    Recupera el historial de Free Cash Flow para un ticker utilizando Financial Modeling Prep.

    Se retorna siempre la lista ordenada de más reciente a más antigua. Si la API devuelve
    menos puntos de los solicitados, se retornan los disponibles.
    """
    cliente = FMPClient()
    historial = cliente.get_free_cash_flow_history(ticker, limit=limite)
    # FMP ya devuelve los datos ordenados del más nuevo al más viejo, pero por las dudas
    historial.sort(key=lambda item: (item.year is None, -(item.year or 0)))
    if len(historial) < minimo:
        # No lanzamos excepción: dejamos que el flujo principal decida cómo proceder.
        return historial
    return historial
