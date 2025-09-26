"""Utilities to interact with the Financial Modeling Prep API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests


class FMPClientError(RuntimeError):
    """Raised when the Financial Modeling Prep client cannot fulfil a request."""


@dataclass(frozen=True)
class FCFEntry:
    """Represents a single historical free cash flow data point."""

    year: Optional[int]
    value: float


@dataclass(frozen=True)
class FMPDerivedMetrics:
    """Financial metrics derived from FMP statements."""

    tax_rate: Optional[float]
    tax_samples: Dict[int, float]
    cost_of_debt: Optional[float]
    cost_samples: Dict[int, float]


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

    def get_income_statements(self, ticker: str, limit: int = 5) -> list:
        """Return annual income statements."""
        ticker = ticker.upper().strip()
        if not ticker:
            raise FMPClientError("El ticker proporcionado no es válido.")
        params = {"symbol": ticker, "period": "annual", "limit": min(limit, 5)}
        data = self._request("stable/income-statement", params=params)

        if isinstance(data, dict):
            error_message = data.get("Error Message") or data.get("error")
            raise FMPClientError(
                f"Financial Modeling Prep devolvió un error al pedir el income statement: {error_message or data}."
            )

        if not isinstance(data, list):
            raise FMPClientError(
                "Financial Modeling Prep devolvió un formato inesperado al pedir el income statement."
            )

        return data

    def get_balance_sheet_statements(self, ticker: str, limit: int = 5) -> list:
        """Return annual balance sheet statements."""
        ticker = ticker.upper().strip()
        if not ticker:
            raise FMPClientError("El ticker proporcionado no es válido.")
        params = {"symbol": ticker, "period": "annual", "limit": min(limit, 5)}
        data = self._request("stable/balance-sheet-statement", params=params)

        if isinstance(data, dict):
            error_message = data.get("Error Message") or data.get("error")
            raise FMPClientError(
                f"Financial Modeling Prep devolvió un error al pedir el balance sheet: {error_message or data}."
            )

        if not isinstance(data, list):
            raise FMPClientError(
                "Financial Modeling Prep devolvió un formato inesperado al pedir el balance sheet."
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


def _extraer_año(data: dict) -> Optional[int]:
    """Obtiene el año numérico desde la respuesta de FMP."""
    raw_year = data.get("calendarYear")
    if raw_year:
        try:
            return int(raw_year)
        except (TypeError, ValueError):
            pass

    raw_date = data.get("date")
    if raw_date:
        try:
            return int(str(raw_date)[:4])
        except (TypeError, ValueError):
            pass

    return None


def obtener_metricas_financieras(ticker: str, limite: int = 5) -> FMPDerivedMetrics:
    """Calcula tasa efectiva y costo de deuda utilizando estados financieros de FMP."""

    cliente = FMPClient()
    income_statements = cliente.get_income_statements(ticker, limit=limite)
    balance_statements = cliente.get_balance_sheet_statements(ticker, limit=limite)

    balance_por_año: Dict[int, float] = {}
    for balance in balance_statements:
        año = _extraer_año(balance)
        if año is None:
            continue

        total_debt = balance.get("totalDebt")
        short_debt = balance.get("shortTermDebt")
        long_debt = balance.get("longTermDebt") or balance.get("longTermDebtTotal")

        deuda_valor: Optional[float] = None
        if total_debt not in (None, ""):
            try:
                deuda_valor = abs(float(total_debt))
            except (TypeError, ValueError):
                deuda_valor = None
        else:
            suma = 0.0
            encontrado = False
            for componente in (short_debt, long_debt):
                if componente in (None, ""):
                    continue
                try:
                    suma += abs(float(componente))
                    encontrado = True
                except (TypeError, ValueError):
                    continue
            if encontrado:
                deuda_valor = suma

        if deuda_valor is None or deuda_valor == 0:
            continue

        balance_por_año[año] = deuda_valor

    tasas_por_año: Dict[int, float] = {}
    costo_por_año: Dict[int, float] = {}

    for income in income_statements:
        año = _extraer_año(income)
        if año is None:
            continue

        impuesto = income.get("incomeTaxExpense")
        ingreso_pre_impuesto = income.get("incomeBeforeTax") or income.get("incomeBeforeIncomeTaxes")
        try:
            impuesto_float = abs(float(impuesto)) if impuesto not in (None, "") else None
            ingreso_float = float(ingreso_pre_impuesto) if ingreso_pre_impuesto not in (None, "") else None
        except (TypeError, ValueError):
            impuesto_float = ingreso_float = None

        if impuesto_float is not None and ingreso_float not in (None, 0):
            tasa = impuesto_float / abs(ingreso_float)
            if 0 <= tasa < 1.5:  # evita valores claramente erróneos
                tasas_por_año[año] = tasa

        interes = income.get("interestExpense")
        if interes in (None, ""):
            interes = income.get("interestExpenseNonOperating")

        try:
            interes_float = abs(float(interes)) if interes not in (None, "") else None
        except (TypeError, ValueError):
            interes_float = None

        deuda = balance_por_año.get(año)
        if interes_float is not None and deuda:
            costo = interes_float / deuda
            if costo >= 0:
                costo_por_año[año] = costo

    tasa_promedio = None
    if tasas_por_año:
        tasa_promedio = sum(tasas_por_año.values()) / len(tasas_por_año)

    costo_promedio = None
    if costo_por_año:
        costo_promedio = sum(costo_por_año.values()) / len(costo_por_año)

    return FMPDerivedMetrics(
        tax_rate=tasa_promedio,
        tax_samples=tasas_por_año,
        cost_of_debt=costo_promedio,
        cost_samples=costo_por_año,
    )
