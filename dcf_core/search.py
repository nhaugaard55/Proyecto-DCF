"""Helpers to search for companies by ticker or name."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, List, Optional

import requests

from .fmp import FMPClient, FMPClientError, FMPSearchResult


@dataclass(frozen=True)
class CompanySearchResult:
    """Normalized representation for company search results."""

    symbol: str
    name: str
    exchange: Optional[str]
    asset_type: Optional[str]


def _from_fmp_result(item: FMPSearchResult) -> CompanySearchResult:
    return CompanySearchResult(
        symbol=item.symbol,
        name=item.name,
        exchange=item.exchange,
        asset_type=item.asset_type,
    )


@lru_cache(maxsize=1)
def _local_company_index() -> List[CompanySearchResult]:
    """Static fallback so the UI keeps working without external APIs."""

    raw_companies = (
        ("AAPL", "Apple Inc.", "NASDAQ"),
        ("MSFT", "Microsoft Corporation", "NASDAQ"),
        ("AMZN", "Amazon.com, Inc.", "NASDAQ"),
        ("GOOGL", "Alphabet Inc. Class A", "NASDAQ"),
        ("GOOG", "Alphabet Inc. Class C", "NASDAQ"),
        ("META", "Meta Platforms, Inc.", "NASDAQ"),
        ("TSLA", "Tesla, Inc.", "NASDAQ"),
        ("NFLX", "Netflix, Inc.", "NASDAQ"),
        ("NVDA", "NVIDIA Corporation", "NASDAQ"),
        ("BABA", "Alibaba Group Holding Limited", "NYSE"),
        ("V", "Visa Inc.", "NYSE"),
        ("MA", "Mastercard Incorporated", "NYSE"),
        ("JPM", "JPMorgan Chase & Co.", "NYSE"),
        ("KO", "The Coca-Cola Company", "NYSE"),
        ("PEP", "PepsiCo, Inc.", "NASDAQ"),
        ("PFE", "Pfizer Inc.", "NYSE"),
        ("DIS", "The Walt Disney Company", "NYSE"),
        ("NKE", "NIKE, Inc.", "NYSE"),
        ("INTC", "Intel Corporation", "NASDAQ"),
        ("ORCL", "Oracle Corporation", "NYSE"),
        ("IBM", "International Business Machines Corporation", "NYSE"),
        ("ADBE", "Adobe Inc.", "NASDAQ"),
        ("CSCO", "Cisco Systems, Inc.", "NASDAQ"),
        ("BAC", "Bank of America Corporation", "NYSE"),
        ("GM", "General Motors Company", "NYSE"),
        ("F", "Ford Motor Company", "NYSE"),
        ("T", "AT&T Inc.", "NYSE"),
        ("VZ", "Verizon Communications Inc.", "NYSE"),
        ("ABNB", "Airbnb, Inc.", "NASDAQ"),
        ("UBER", "Uber Technologies, Inc.", "NYSE"),
        ("SHOP", "Shopify Inc.", "NYSE"),
    )

    return [
        CompanySearchResult(symbol=symbol, name=name, exchange=exchange, asset_type=None)
        for symbol, name, exchange in raw_companies
    ]


def _filter_results(results: Iterable[CompanySearchResult], query: str) -> List[CompanySearchResult]:
    query_lower = query.lower()
    filtered: List[CompanySearchResult] = []
    seen = set()

    for item in results:
        if not item:
            continue

        symbol = (item.symbol or "").lower()
        name = (item.name or "").lower()
        exchange = (item.exchange or "").lower()

        if (
            query_lower in symbol
            or query_lower in name
            or query_lower in exchange
        ):
            key = (item.symbol or "").upper()
            if key and key not in seen:
                filtered.append(item)
                seen.add(key)

    return filtered


def _search_with_yahoo(query: str, limit: int) -> List[CompanySearchResult]:
    url = "https://query1.finance.yahoo.com/v1/finance/search"
    params = {
        "q": query,
        "quotesCount": limit,
        "lang": "en-US",
        "region": "US",
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DCFApp/1.0)"},
        )
        response.raise_for_status()
    except requests.RequestException:
        return []

    try:
        payload = response.json()
    except ValueError:
        return []

    results: List[CompanySearchResult] = []
    for quote in payload.get("quotes", [])[:limit]:
        symbol = (quote.get("symbol") or "").strip()
        if not symbol:
            continue

        name = (
            quote.get("shortname")
            or quote.get("longname")
            or quote.get("name")
            or symbol
        )
        exchange = quote.get("exchDisp") or quote.get("exchangeShortName")
        asset_type = quote.get("quoteType") or quote.get("typeDisp")

        results.append(
            CompanySearchResult(
                symbol=symbol.upper(),
                name=name,
                exchange=exchange,
                asset_type=asset_type,
            )
        )

    return _filter_results(results, query)


def _search_locally(query: str) -> List[CompanySearchResult]:
    return _filter_results(_local_company_index(), query)


def search_companies(query: str, limit: int = 8) -> List[CompanySearchResult]:
    """Return company matches using FMP when possible, otherwise fall back to Yahoo search."""

    cleaned_query = query.strip()
    if not cleaned_query:
        return []

    normalized_limit = max(1, min(limit, 20))

    resultados: List[CompanySearchResult] = []
    api_key = os.environ.get("FMP_API_KEY")

    if api_key:
        try:
            cliente = FMPClient(api_key=api_key)
            resultados_fmp = cliente.search_companies(cleaned_query, limit=normalized_limit * 2)
            resultados.extend(_from_fmp_result(item) for item in resultados_fmp)
        except FMPClientError:
            resultados = []
    else:
        # Evita levantar una excepción cada vez que falta la API key
        resultados = []

    filtrados = _filter_results(resultados, cleaned_query)
    if filtrados:
        return filtrados[:normalized_limit]

    yahoo = _search_with_yahoo(cleaned_query, limit=normalized_limit * 2)
    if yahoo:
        return yahoo[:normalized_limit]

    locales = _search_locally(cleaned_query)
    return locales[:normalized_limit]
