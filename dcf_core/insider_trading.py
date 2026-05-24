"""Obtención y normalización de transacciones de insiders.

El módulo consulta primero Finnhub y usa Financial Modeling Prep como fallback.
Está diseñado para poder importarse fuera de Django: no usa settings, ORM ni
cache framework.
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

import requests


_FINNHUB_URL = "https://finnhub.io/api/v1/stock/insider-transactions"
_FMP_URL = "https://financialmodelingprep.com/stable/insider-trading"
_SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
_SEC_HEADERS = {"User-Agent": "DCFAnalyzer/1.0 contact@example.com"}
_REQUEST_TIMEOUT = 3
_SEC_ENRICHMENT_BUDGET_SECONDS = 4.5
_CACHE_TTL_SECONDS = 60 * 60
_CACHE_SCHEMA_VERSION = "roles-v6"
_MAX_TRANSACTIONS = 20
_LOOKBACK_DAYS = 180
_SCORE_LOOKBACK_DAYS = 90
_ADVERTENCIA = (
    "Las compras de insiders son señal más confiable que las ventas, que pueden obedecer "
    "a razones personales (diversificación, impuestos, planes 10b5-1) no relacionadas "
    "con la visión sobre la empresa."
)

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SEC_CIK_CACHE: dict[str, int] = {}
_SEC_SUBMISSIONS_CACHE: dict[int, dict[str, Any]] = {}


def get_insider_trading(ticker: str) -> dict:
    """Devuelve actividad reciente de insiders para un ticker.

    Intenta Finnhub como fuente primaria y FMP como respaldo. Si las APIs no
    responden, faltan credenciales o no hay actividad reciente, devuelve un
    payload no disponible sin lanzar excepciones para no romper el análisis DCF.
    """

    symbol = (ticker or "").strip().upper()
    if not symbol:
        return _sin_datos(symbol)

    cache_key = f"{_CACHE_SCHEMA_VERSION}:{symbol}"
    cached = _CACHE.get(cache_key)
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
            if fuente == "finnhub":
                _enriquecer_cargos_desde_fmp(symbol, transacciones)
                _enriquecer_cargos_desde_sec(symbol, transacciones)
            _propagar_cargos_por_insider(transacciones)
            _limpiar_campos_internos(transacciones)
            payload = _con_datos(symbol, fuente, transacciones)
            _CACHE[cache_key] = (time.time(), payload)
            return payload
        except Exception:
            continue

    payload = _sin_datos(symbol)
    _CACHE[cache_key] = (time.time(), payload)
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
        tx["venta_relacionada_ejercicio"] = bool(tx.get("venta_relacionada_ejercicio"))
        tx["plan_automatico"] = bool(tx.get("plan_automatico"))
        tx["tipo_extendido"] = tx.get("tipo_extendido") or tx["tipo"]
        tx["tipo_label"] = _tipo_label(tx["tipo"])
        tx["valor_total_display"] = _format_money_short(tx.get("valor_total"))
        tx["shares_display"] = _format_number(tx.get("shares"))
        tx["precio_display"] = _format_price(tx.get("precio"))
        transacciones.append(tx)

    _marcar_ventas_post_ejercicio(transacciones)
    for tx in transacciones:
        tx.pop("_raw_text", None)
    transacciones.sort(key=lambda tx: tx["fecha"], reverse=True)
    return transacciones[:_MAX_TRANSACTIONS]


def _normalizar_finnhub(item: dict[str, Any]) -> dict[str, Any]:
    """Adapta una transacción de Finnhub al formato interno."""

    shares = _to_number(
        _first_present(
            item.get("change"),
            item.get("transactionShares"),
            item.get("securitiesTransacted"),
            item.get("shares"),
            item.get("share"),
        )
    )
    precio = _to_number(
        _first_present(
            item.get("transactionPrice"),
            item.get("price"),
            item.get("transaction_price"),
        )
    )
    valor_total = _calcular_valor_total(item, shares, precio)
    raw_text = _raw_text(item)
    raw_cargo = (
        item.get("title")
        or item.get("officerTitle")
        or item.get("officer_title")
        or item.get("relationship")
        or item.get("relation")
    )
    cargo = _extraer_cargo_finnhub(item)
    cargo_detalle = _texto(raw_cargo) if raw_cargo else (cargo if cargo != "N/D" else "")

    return {
        "fecha": item.get("transactionDate") or item.get("filingDate") or item.get("date"),
        "insider_nombre": _texto(item.get("name") or item.get("insiderName") or item.get("reportingName")),
        "insider_cargo": cargo,
        "insider_cargo_detalle": cargo_detalle,
        "insider_cargo_fuente": "finnhub" if cargo != "N/D" else "",
        "tipo": _clasificar_tipo(item.get("transactionCode") or item.get("code") or item.get("transactionType")),
        "tipo_extendido": _clasificar_tipo(item.get("transactionCode") or item.get("code") or item.get("transactionType")),
        "plan_automatico": _es_plan_automatico(raw_text),
        "shares": abs(shares) if shares is not None else None,
        "precio": precio,
        "valor_total": valor_total,
        "shares_restantes": _to_number(
            _first_present(
                item.get("shareOwnedFollowingTransaction"),
                item.get("sharesOwnedFollowingTransaction"),
                item.get("securitiesOwned"),
                item.get("share"),
            )
        ),
        "_issuer_symbol": item.get("symbol"),
        "_filing_id": item.get("id") or item.get("accessionNumber") or item.get("accession"),
        "_raw_text": raw_text,
    }


def _normalizar_fmp(item: dict[str, Any]) -> dict[str, Any]:
    """Adapta una transacción de FMP al formato interno."""

    shares = _to_number(
        item.get("securitiesTransacted")
        or item.get("transactionShares")
        or item.get("shares")
        or item.get("acquistionOrDisposition")
    )
    precio = _to_number(_first_present(item.get("price"), item.get("transactionPrice")))
    valor_total = _calcular_valor_total(item, shares, precio)
    raw_text = _raw_text(item)
    raw_cargo = item.get("officerTitle") or item.get("title") or item.get("typeOfOwner")
    cargo = _normalizar_cargo(raw_cargo)

    return {
        "fecha": item.get("transactionDate") or item.get("filingDate") or item.get("date"),
        "insider_nombre": _texto(item.get("reportingName") or item.get("name") or item.get("insiderName")),
        "insider_cargo": cargo,
        "insider_cargo_detalle": _texto(raw_cargo) if cargo != "N/D" else "",
        "insider_cargo_fuente": "fmp" if cargo != "N/D" else "",
        "tipo": _clasificar_tipo(item.get("transactionType") or item.get("transactionCode") or item.get("code")),
        "tipo_extendido": _clasificar_tipo(item.get("transactionType") or item.get("transactionCode") or item.get("code")),
        "plan_automatico": _es_plan_automatico(raw_text),
        "shares": abs(shares) if shares is not None else None,
        "precio": precio,
        "valor_total": valor_total,
        "shares_restantes": _to_number(
            _first_present(
                item.get("securitiesOwned"),
                item.get("sharesOwnedFollowingTransaction"),
                item.get("shareOwnedFollowingTransaction"),
            )
        ),
        "_issuer_symbol": item.get("symbol"),
        "_filing_id": item.get("id") or item.get("accessionNumber") or item.get("accession"),
        "_raw_text": raw_text,
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
        if (_parse_fecha(tx.get("fecha")) or cutoff) >= cutoff and tx.get("tipo") in {"compra", "venta"}
    ]

    compras = [tx for tx in recientes if tx.get("tipo") == "compra"]
    ventas = [tx for tx in recientes if tx.get("tipo") == "venta"]
    valor_compras = sum(_safe_float(tx.get("valor_total")) for tx in compras)
    valor_ventas = sum(_safe_float(tx.get("valor_total")) for tx in ventas)
    valor_ventas_ajustado = sum(_safe_float(tx.get("valor_total")) * _peso_venta_score(tx) for tx in ventas)
    ventas_post_ejercicio = sum(
        _safe_float(tx.get("valor_total")) for tx in ventas if tx.get("venta_relacionada_ejercicio")
    )
    ventas_automaticas = sum(
        _safe_float(tx.get("valor_total")) for tx in ventas if tx.get("plan_automatico")
    )
    ventas_normales = sum(
        _safe_float(tx.get("valor_total"))
        for tx in ventas
        if not tx.get("venta_relacionada_ejercicio") and not tx.get("plan_automatico")
    )
    ventas_compensacion = sum(
        _safe_float(tx.get("valor_total"))
        for tx in ventas
        if tx.get("venta_relacionada_ejercicio") or tx.get("plan_automatico")
    )
    total_valor = valor_compras + valor_ventas_ajustado

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
        "valor_ventas_ajustado_usd": valor_ventas_ajustado,
        "ventas_ajustadas_usd": valor_ventas_ajustado,
        "ventas_normales_usd": ventas_normales,
        "ventas_post_ejercicio_usd": ventas_post_ejercicio,
        "ventas_automaticas_usd": ventas_automaticas,
        "valor_compras_display": _format_money_short(valor_compras),
        "valor_ventas_display": _format_money_short(valor_ventas),
        "valor_ventas_ajustado_display": _format_money_short(valor_ventas_ajustado),
        "ventas_normales_display": _format_money_short(ventas_normales),
        "ventas_post_ejercicio_display": _format_money_short(ventas_post_ejercicio),
        "ventas_automaticas_display": _format_money_short(ventas_automaticas),
        "ratio_compras": ratio,
        "ratio_compras_pct": round(ratio * 100),
        "porcentaje_ventas_ajustadas_sobre_brutas": round(
            (valor_ventas_ajustado / valor_ventas) * 100
        ) if valor_ventas else 0,
        "advertencia_ventas_compensacion": valor_ventas > 0 and (ventas_compensacion / valor_ventas) > 0.50,
        "porcentaje_ventas_compensacion": round((ventas_compensacion / valor_ventas) * 100) if valor_ventas else 0,
    }

    return {"score_sentimiento": score, **meta, "resumen": resumen}


def _marcar_ventas_post_ejercicio(transacciones: list[dict[str, Any]]) -> None:
    """Marca ventas cercanas a ejercicios de opciones del mismo insider."""

    ejercicios = [
        tx for tx in transacciones
        if tx.get("tipo") == "ejercicio" and tx.get("insider_nombre") and _parse_fecha(tx.get("fecha"))
    ]
    if not ejercicios:
        return

    for venta in transacciones:
        if venta.get("tipo") != "venta" or not venta.get("insider_nombre"):
            continue
        venta_fecha = _parse_fecha(venta.get("fecha"))
        if venta_fecha is None:
            continue
        venta_nombre = _normalizar_nombre_insider(venta.get("insider_nombre"))
        for ejercicio in ejercicios:
            if venta_nombre != _normalizar_nombre_insider(ejercicio.get("insider_nombre")):
                continue
            ejercicio_fecha = _parse_fecha(ejercicio.get("fecha"))
            if ejercicio_fecha is None:
                continue
            if abs((venta_fecha - ejercicio_fecha).days) <= 2:
                venta["venta_relacionada_ejercicio"] = True
                venta["tipo_extendido"] = "venta post-ejercicio"
                break


def _peso_venta_score(tx: dict[str, Any]) -> float:
    """Devuelve el peso de una venta para el score de sentimiento."""

    if tx.get("plan_automatico"):
        return 0.15
    if tx.get("venta_relacionada_ejercicio"):
        return 0.25
    return 1.0


def _clasificar_tipo(code: Any) -> str:
    """Clasifica códigos Form 4 en categorías simples."""

    value = str(code or "").strip().upper()
    if value == "P" or value.startswith("P-"):
        return "compra"
    if value == "S" or value.startswith("S-"):
        return "venta"
    if value == "M" or value.startswith("M-"):
        return "ejercicio"
    if value == "A" or value.startswith("A-"):
        return "adjudicacion"
    if value == "F" or value.startswith("F-"):
        return "retencion_impuestos"
    if value == "G" or value.startswith("G-"):
        return "donacion"
    if value == "D" or value.startswith("D-"):
        return "disposicion"
    if "PURCHASE" in value or "BUY" in value or "ACQUISITION" in value:
        return "compra"
    if "SALE" in value or "SELL" in value or "DISPOSITION" in value:
        return "venta"
    if "OPTION" in value or "EXERCISE" in value:
        return "ejercicio"
    if "TAX" in value or "WITHHOLD" in value:
        return "retencion_impuestos"
    if "GRANT" in value or "AWARD" in value:
        return "adjudicacion"
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
    if "SVP" in lookup or "VP" in lookup or "VICE PRESIDENT" in lookup:
        return "VP"
    if "PRESIDENT" in lookup:
        return "Presidente"
    if "GENERAL COUNSEL" in lookup or "CLO" in lookup:
        return "Counsel"
    if "10%" in lookup or "TEN PERCENT" in lookup:
        return "Accionista >10%"
    if "OFFICER" in lookup:
        return "Ejecutivo"
    return texto[:30] if texto else "N/D"


def _extraer_cargo_finnhub(item: dict[str, Any]) -> str:
    """Obtiene el cargo desde campos explícitos o flags disponibles en Finnhub."""

    cargo = _normalizar_cargo(
        item.get("title")
        or item.get("officerTitle")
        or item.get("officer_title")
        or item.get("relationship")
        or item.get("relation")
    )
    if cargo != "N/D":
        return cargo

    if item.get("isCeo") or item.get("isCEO"):
        return "CEO"
    if item.get("isCfo") or item.get("isCFO"):
        return "CFO"
    if item.get("isCoo") or item.get("isCOO"):
        return "COO"
    if item.get("isDirector"):
        return "Director"
    if item.get("isOfficer"):
        return "Ejecutivo"
    if item.get("isTenPercentOwner"):
        return "Accionista >10%"
    return "N/D"


def _enriquecer_cargos_desde_fmp(ticker: str, transacciones: list[dict[str, Any]]) -> None:
    """Completa cargos faltantes con FMP sin alterar la fuente ni el score."""

    if not any((tx.get("insider_cargo") in (None, "", "N/D")) for tx in transacciones):
        return

    try:
        fmp_items = _fetch_fmp(ticker)
    except Exception:
        return

    cargos_por_nombre: dict[str, tuple[str, str]] = {}
    for item in fmp_items:
        tx = _normalizar_fmp(item)
        nombre_key = _normalizar_nombre_insider(tx.get("insider_nombre"))
        cargo = tx.get("insider_cargo")
        if nombre_key and cargo and cargo != "N/D":
            cargos_por_nombre[nombre_key] = (cargo, tx.get("insider_cargo_detalle") or cargo)

    if not cargos_por_nombre:
        return

    for tx in transacciones:
        if tx.get("insider_cargo") not in (None, "", "N/D"):
            continue
        nombre_key = _normalizar_nombre_insider(tx.get("insider_nombre"))
        cargo_info = cargos_por_nombre.get(nombre_key)
        if cargo_info:
            cargo, cargo_detalle = cargo_info
            tx["insider_cargo"] = cargo
            tx["insider_cargo_detalle"] = cargo_detalle
            tx["insider_cargo_fuente"] = "fmp"


def _enriquecer_cargos_desde_sec(ticker: str, transacciones: list[dict[str, Any]]) -> None:
    """Completa cargos faltantes escaneando Form 4s recientes de SEC por CIK."""

    faltantes = [tx for tx in transacciones if tx.get("insider_cargo") in (None, "", "N/D")]
    if not faltantes:
        return

    deadline = time.monotonic() + _SEC_ENRICHMENT_BUDGET_SECONDS

    for symbol in _sec_symbol_candidates(ticker, transacciones):
        if time.monotonic() >= deadline:
            return

        cik = _sec_cik_for_ticker(symbol, deadline)
        if cik is None:
            continue

        filings = _sec_recent_filings(cik, deadline)
        if not filings:
            continue

        cargos = _construir_mapa_cargos_sec(cik, filings, deadline)
        if not cargos:
            continue

        for tx in transacciones:
            if tx.get("insider_cargo") not in (None, "", "N/D"):
                continue
            nombre_key = _normalizar_nombre_insider(tx.get("insider_nombre"))
            cargo_info = cargos.get(nombre_key)
            if cargo_info:
                cargo, detalle = cargo_info
                tx["insider_cargo"] = cargo
                tx["insider_cargo_detalle"] = detalle
                tx["insider_cargo_fuente"] = "sec"

        if not any(tx.get("insider_cargo") in (None, "", "N/D") for tx in transacciones):
            return


def _construir_mapa_cargos_sec(
    cik: int,
    filings: dict[str, Any],
    deadline: float,
) -> dict[str, tuple[str, str]]:
    """Escanea Form 4s recientes y devuelve {nombre_normalizado: (cargo, detalle)}."""

    all_accessions = filings.get("accessionNumber") or []
    all_forms = filings.get("form") or []
    all_docs = filings.get("primaryDocument") or []

    form4_items = [
        (acc, doc)
        for acc, form, doc in zip(all_accessions, all_forms, all_docs)
        if form in ("4", "4/A") and acc and doc
    ][:20]

    if not form4_items:
        return {}

    cargos: dict[str, tuple[str, str]] = {}
    max_workers = min(4, len(form4_items))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {
            executor.submit(_fetch_sec_form4_nombre_cargo, cik, acc, doc, deadline): acc
            for acc, doc in form4_items
            if time.monotonic() < deadline
        }
        timeout = max(0.1, deadline - time.monotonic())
        try:
            for future in as_completed(future_map, timeout=timeout):
                try:
                    result = future.result()
                except Exception:
                    continue
                if result:
                    nombre_norm, cargo, detalle = result
                    cargos.setdefault(nombre_norm, (cargo, detalle))
        except TimeoutError:
            pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return cargos


def _fetch_sec_form4_nombre_cargo(
    cik: int,
    accession: str,
    document: str,
    deadline: float,
) -> tuple[str, str, str] | None:
    """Descarga un Form 4 y extrae (nombre_normalizado, cargo, detalle)."""

    if time.monotonic() >= deadline:
        return None

    url = _SEC_ARCHIVES_URL.format(
        cik=cik,
        accession=accession.replace("-", ""),
        document=document,
    )
    try:
        response = requests.get(
            url,
            headers=_SEC_HEADERS,
            timeout=max(0.2, min(1.0, deadline - time.monotonic())),
        )
        if response.status_code != 200:
            return None
    except Exception:
        return None

    return _extraer_nombre_cargo_sec(response.text)


def _extraer_nombre_cargo_sec(text: str) -> tuple[str, str, str] | None:
    """Extrae (nombre_normalizado, cargo, detalle) desde XML o HTML de un Form 4."""

    # ── Formato XML (Form 4 raw) ───────────────────────────────────────────────
    name_match = re.search(r"<rptOwnerName[^>]*>(.*?)</rptOwnerName>", text, re.IGNORECASE | re.DOTALL)
    if name_match:
        nombre = _strip_html(name_match.group(1)).strip()
        nombre_norm = _normalizar_nombre_insider(nombre)
        if not nombre_norm:
            return None

        title_match = re.search(r"<officerTitle[^>]*>(.*?)</officerTitle>", text, re.IGNORECASE | re.DOTALL)
        if title_match:
            raw_title = _strip_html(title_match.group(1)).strip()
            if raw_title:
                cargo = _normalizar_cargo(raw_title)
                if cargo != "N/D":
                    return nombre_norm, cargo, raw_title

        is_director = re.search(r"<isDirector[^>]*>1</isDirector>", text, re.IGNORECASE)
        if is_director:
            return nombre_norm, "Director", "Miembro del directorio"

        is_owner = re.search(r"<isTenPercentOwner[^>]*>1</isTenPercentOwner>", text, re.IGNORECASE)
        if is_owner:
            return nombre_norm, "Accionista >10%", "Accionista >10%"

        is_officer = re.search(r"<isOfficer[^>]*>1</isOfficer>", text, re.IGNORECASE)
        if is_officer:
            cargo_info = _extraer_cargo_sec_html_detalle(text)
            if cargo_info:
                return nombre_norm, cargo_info[0], cargo_info[1]
            return nombre_norm, "Ejecutivo", "Ejecutivo"

        return None

    # ── Formato HTML (XSLT-rendered) ──────────────────────────────────────────
    name_html = re.search(
        r"1\.\s*Name[^<]*(?:<[^>]+>)*.*?<a[^>]*>([^<]{3,60})</a>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not name_html:
        return None
    nombre = name_html.group(1).strip()
    nombre_norm = _normalizar_nombre_insider(nombre)
    if not nombre_norm:
        return None

    cargo_info = _extraer_cargo_sec_html_detalle(text)
    if cargo_info:
        return nombre_norm, cargo_info[0], cargo_info[1]

    director_html = re.search(
        r"<span[^>]*>\s*X\s*</span>\s*</td>\s*<td[^>]*>\s*Director\s*</td>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if director_html:
        return nombre_norm, "Director", "Miembro del directorio"

    owner_html = re.search(
        r"<span[^>]*>\s*X\s*</span>\s*</td>\s*<td[^>]*>\s*10%\s*Owner\s*</td>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if owner_html:
        return nombre_norm, "Accionista >10%", "Accionista >10%"

    return None


def _propagar_cargos_por_insider(transacciones: list[dict[str, Any]]) -> None:
    """Rellena cargos faltantes usando otras transacciones del mismo insider."""

    cargos_por_nombre: dict[str, tuple[str, str, str]] = {}
    for tx in transacciones:
        nombre_key = _normalizar_nombre_insider(tx.get("insider_nombre"))
        cargo = tx.get("insider_cargo")
        if not nombre_key or cargo in (None, "", "N/D"):
            continue
        detalle = tx.get("insider_cargo_detalle") or cargo
        fuente = tx.get("insider_cargo_fuente") or "misma ventana"
        cargos_por_nombre.setdefault(nombre_key, (cargo, detalle, fuente))

    for tx in transacciones:
        if tx.get("insider_cargo") not in (None, "", "N/D"):
            continue
        nombre_key = _normalizar_nombre_insider(tx.get("insider_nombre"))
        cargo_info = cargos_por_nombre.get(nombre_key)
        if not cargo_info:
            continue
        cargo, detalle, fuente = cargo_info
        tx["insider_cargo"] = cargo
        tx["insider_cargo_detalle"] = detalle
        tx["insider_cargo_fuente"] = fuente


def _sec_symbol_candidates(ticker: str, transacciones: list[dict[str, Any]]) -> list[str]:
    """Devuelve tickers posibles para resolver CIK, incluyendo clases alternativas reportadas."""

    candidates: list[str] = []
    for value in [ticker, *(tx.get("_issuer_symbol") for tx in transacciones)]:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in candidates:
            candidates.append(symbol)
    return candidates


def _sec_cik_for_ticker(ticker: str, deadline: float) -> int | None:
    """Resuelve el CIK de SEC para un ticker usando caché en memoria."""

    symbol = (ticker or "").strip().upper()
    if not symbol:
        return None
    if symbol in _SEC_CIK_CACHE:
        return _SEC_CIK_CACHE[symbol]
    if time.monotonic() >= deadline:
        return None

    try:
        response = requests.get(
            _SEC_COMPANY_TICKERS_URL,
            headers=_SEC_HEADERS,
            timeout=max(0.2, min(1.0, deadline - time.monotonic())),
        )
        if response.status_code != 200:
            return None
        data = response.json()
    except Exception:
        return None

    iterable = data.values() if isinstance(data, dict) else data
    for item in iterable:
        if not isinstance(item, dict):
            continue
        if str(item.get("ticker") or "").strip().upper() == symbol:
            try:
                cik = int(item.get("cik_str"))
            except (TypeError, ValueError):
                return None
            _SEC_CIK_CACHE[symbol] = cik
            return cik
    return None


def _sec_recent_filings(cik: int, deadline: float) -> dict[str, Any] | None:
    """Obtiene filings recientes de SEC con caché por CIK."""

    if cik in _SEC_SUBMISSIONS_CACHE:
        return _SEC_SUBMISSIONS_CACHE[cik]
    if time.monotonic() >= deadline:
        return None

    try:
        response = requests.get(
            _SEC_SUBMISSIONS_URL.format(cik=cik),
            headers=_SEC_HEADERS,
            timeout=max(0.2, min(1.0, deadline - time.monotonic())),
        )
        if response.status_code != 200:
            return None
        data = response.json().get("filings", {}).get("recent", {})
    except Exception:
        return None

    if isinstance(data, dict):
        _SEC_SUBMISSIONS_CACHE[cik] = data
        return data
    return None


def _sec_cargo_for_accession(
    cik: int,
    accession: str,
    filings: dict[str, Any],
    deadline: float,
) -> tuple[str, str] | None:
    """Extrae el cargo del reporting owner desde el documento primario Form 4."""

    if not accession:
        return None
    accession_numbers = filings.get("accessionNumber") or []
    documents = filings.get("primaryDocument") or []
    try:
        index = accession_numbers.index(accession)
        document = documents[index]
    except (ValueError, IndexError):
        return None

    if not document:
        return None
    if time.monotonic() >= deadline:
        return None

    url = _SEC_ARCHIVES_URL.format(
        cik=cik,
        accession=accession.replace("-", ""),
        document=document,
    )
    try:
        response = requests.get(
            url,
            headers=_SEC_HEADERS,
            timeout=max(0.2, min(1.0, deadline - time.monotonic())),
        )
        if response.status_code != 200:
            return None
    except Exception:
        return None

    return _extraer_cargo_sec_html_detalle(response.text)


def _extraer_cargo_sec_html(html: str) -> str | None:
    """Extrae el título de officer desde HTML/XML de un Form 4."""

    cargo_info = _extraer_cargo_sec_html_detalle(html)
    return cargo_info[0] if cargo_info else None


def _extraer_cargo_sec_html_detalle(html: str) -> tuple[str, str] | None:
    """Extrae cargo normalizado y título exacto desde un Form 4."""

    text = html or ""
    match = re.search(
        r"Officer \(give title below\).*?<td[^>]*style=\"color:\s*blue\"[^>]*>(.*?)</td>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        detalle = _strip_html(match.group(1))
        cargo = _normalizar_cargo(detalle)
        if cargo != "N/D":
            return cargo, detalle

    director_block = re.search(
        r"<span[^>]*>\s*X\s*</span>\s*</td>\s*<td[^>]*>\s*Director\s*</td>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if director_block:
        return "Director", "Miembro del directorio"

    owner_block = re.search(
        r"<span[^>]*>\s*X\s*</span>\s*</td>\s*<td[^>]*>\s*10%\s*Owner\s*</td>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if owner_block:
        return "Accionista >10%", "Accionista >10%"

    return None


def _strip_html(value: str) -> str:
    """Elimina tags simples y entidades HTML."""

    return unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


def _limpiar_campos_internos(transacciones: list[dict[str, Any]]) -> None:
    """Quita campos auxiliares que no forman parte del contrato público."""

    for tx in transacciones:
        tx.pop("_raw_text", None)
        tx.pop("_filing_id", None)
        tx.pop("_issuer_symbol", None)


def _normalizar_nombre_insider(value: Any) -> str:
    """Normaliza nombres para comparar transacciones del mismo insider."""

    return " ".join(_texto(value).upper().replace(",", " ").split())


def _raw_text(item: dict[str, Any]) -> str:
    """Concatena texto crudo del filing para detectar planes automáticos."""

    partes = []
    for value in item.values():
        if isinstance(value, (str, int, float, bool)):
            partes.append(str(value))
        elif isinstance(value, list):
            partes.extend(str(v) for v in value if isinstance(v, (str, int, float, bool)))
        elif isinstance(value, dict):
            partes.extend(str(v) for v in value.values() if isinstance(v, (str, int, float, bool)))
    return " ".join(partes)


def _es_plan_automatico(raw_text: str) -> bool:
    """Detecta indicios de venta automática, tax withholding o plan 10b5-1."""

    texto = (raw_text or "").lower()
    patrones = (
        "10b5-1",
        "planned sale",
        "automatic sale",
        "sell to cover",
        "tax withholding",
    )
    return any(patron in texto for patron in patrones)


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


def _first_present(*values: Any) -> Any:
    """Devuelve el primer valor presente sin descartar ceros válidos."""

    for value in values:
        if value not in (None, "", "N/D"):
            return value
    return None


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
        "adjudicacion": "Adjudicación",
        "retencion_impuestos": "Retención imp.",
        "donacion": "Donación",
        "disposicion": "Disposición",
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
