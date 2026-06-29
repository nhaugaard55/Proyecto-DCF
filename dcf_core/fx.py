"""
Conversión de moneda extranjera para el DCF.

CONVENCIÓN (invariante de todo el módulo):
    rate  = unidades de moneda local por 1 USD
    conversión: value_usd = value_local / rate

Fuente de rates: yfinance, par "{CURRENCY}=X"
    ARS=X → Close ≈ 1477  → 1477 ARS por 1 USD   → value_usd = value_ars / 1477
    BRL=X → Close ≈ 5.7   → 5.7 BRL por 1 USD    → value_usd = value_brl / 5.7

FMP historical-price-full no está disponible en el plan actual (legacy).
Histórico vía yfinance: .history(period="6y", interval="1mo"), columna "Close".

Detección de moneda (en orden de prioridad):
    1. yfinance info["financialCurrency"] — moneda de los estados financieros.
       NO usar info["currency"], que es la moneda de trading (USD para ADRs en NYSE).
    2. FMP reportedCurrency en income statement (fallback si FMP disponible).
    3. "USD" por default (sin conversión).

Guard: si moneda == "USD" no se realiza ninguna llamada externa de FX.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Union
import datetime

import yfinance as yf

logger = logging.getLogger(__name__)


def detectar_moneda_yfinance(info: dict) -> str:
    """
    Detecta la moneda de reporte desde el dict info de yfinance.

    Usa "financialCurrency" (moneda de estados financieros, ej: "ARS" para CRESY).
    NO usa "currency", que es la moneda de trading y siempre es "USD" para ADRs.

    Requiere el dict info ya fetcheado por el caller (sin llamadas externas).
    """
    moneda = info.get("financialCurrency")
    if moneda and isinstance(moneda, str) and moneda.strip():
        resultado = moneda.strip().upper()
        logger.info(f"[FX] financialCurrency yfinance = {resultado!r}")
        return resultado
    return "USD"


def detectar_moneda_fmp(ticker: str, api_key: Optional[str] = None) -> str:
    """
    Lee el primer income statement del ticker en FMP y retorna reportedCurrency.
    Retorna "USD" si la llamada falla o el campo no está disponible.

    Usar como fallback cuando detectar_moneda_yfinance no está disponible.
    Para muchos ADRs (ej: CRESY), FMP devuelve 402 y este método también retorna "USD".
    """
    try:
        from .fmp import FMPClient
        cliente = FMPClient(api_key=api_key)
        statements = cliente.get_income_statements(ticker, limit=1)
        if statements and isinstance(statements, list):
            moneda = statements[0].get("reportedCurrency") or "USD"
            moneda = moneda.strip().upper()
            logger.info(f"[FX] {ticker}: reportedCurrency FMP = {moneda!r}")
            return moneda
    except Exception as exc:
        logger.warning(f"[FX] No se pudo detectar moneda de {ticker} vía FMP: {exc}")
    return "USD"


def obtener_fx_spot(moneda: str) -> float:
    """
    Retorna el rate spot: unidades de moneda local por 1 USD.
    Si moneda == "USD", retorna 1.0 sin ninguna llamada externa.

    Log explícito del valor crudo para verificación de dirección.
    """
    if moneda.upper() == "USD":
        return 1.0

    par = f"{moneda.upper()}=X"
    try:
        ticker_obj = yf.Ticker(par)
        rate_raw = None
        try:
            rate_raw = ticker_obj.fast_info["lastPrice"]
        except Exception:
            rate_raw = ticker_obj.info.get("regularMarketPrice")

        logger.info(
            f"[FX spot] {par} raw={rate_raw!r} "
            f"— convención: {moneda} por 1 USD "
            f"(value_usd = value_{moneda.lower()} / {rate_raw})"
        )

        if rate_raw is not None and float(rate_raw) > 0:
            return float(rate_raw)
    except Exception as exc:
        logger.warning(f"[FX spot] yfinance falló para {par}: {exc}")

    logger.warning(
        f"[FX spot] No se pudo obtener rate para {moneda}. "
        "Usando 1.0 — valores NO serán convertidos."
    )
    return 1.0


def obtener_fx_historico(moneda: str) -> dict[str, float]:
    """
    Retorna rates mensuales históricos: {"YYYY-MM": rate}.
    rate = unidades de moneda local por 1 USD (misma convención que spot).

    Si moneda == "USD", retorna {} sin ninguna llamada externa.
    Cubre hasta 6 años hacia atrás para cubrir series de 5 FY.

    Fallback: si yfinance falla, retorna {} y el caller debe usar spot.
    """
    if moneda.upper() == "USD":
        return {}

    par = f"{moneda.upper()}=X"
    try:
        ticker_obj = yf.Ticker(par)
        hist = ticker_obj.history(period="6y", interval="1mo")

        if hist.empty or "Close" not in hist.columns:
            logger.warning(f"[FX hist] yfinance devolvió datos vacíos para {par}.")
            return {}

        result: dict[str, float] = {}
        for ts, row in hist.iterrows():
            rate = float(row["Close"])
            if rate > 0:
                mes = ts.strftime("%Y-%m")
                result[mes] = rate

        # Log de muestra para verificar dirección
        muestra = list(result.items())
        logger.info(
            f"[FX hist] {par}: {len(result)} meses disponibles. "
            f"Muestra (más antiguo → más reciente): "
            f"{muestra[:2]} ... {muestra[-2:]}"
        )

        return result

    except Exception as exc:
        logger.warning(f"[FX hist] yfinance histórico falló para {par}: {exc}")
        return {}


def buscar_rate_por_fecha(
    fx_mensual: dict[str, float],
    fecha: Union[str, datetime.date, datetime.datetime, int],
    fallback_spot: float,
) -> float:
    """
    Busca el rate mensual más cercano a `fecha` en fx_mensual.
    Si no hay datos en el dict, retorna fallback_spot.

    `fecha` puede ser:
      - str "YYYY-MM-DD" o "YYYY-MM"
      - datetime.date / datetime.datetime
      - int (año) → usa diciembre de ese año

    La búsqueda es: primero el mes exacto, luego el mes anterior,
    luego el mes siguiente (tolerancia ±2 meses), luego spot.
    """
    if not fx_mensual:
        return fallback_spot

    # Normalizar a "YYYY-MM"
    if isinstance(fecha, int):
        clave_base = f"{fecha}-12"
    elif isinstance(fecha, (datetime.date, datetime.datetime)):
        clave_base = fecha.strftime("%Y-%m")
    elif isinstance(fecha, str):
        clave_base = fecha[:7]  # "YYYY-MM"
    else:
        return fallback_spot

    # Buscar en ±2 meses
    try:
        año, mes = int(clave_base[:4]), int(clave_base[5:7])
        base_dt = datetime.date(año, mes, 1)
    except (ValueError, IndexError):
        return fallback_spot

    for delta in (0, -1, 1, -2, 2):
        try:
            candidato = base_dt + datetime.timedelta(days=delta * 31)
            clave = candidato.strftime("%Y-%m")
            if clave in fx_mensual:
                return fx_mensual[clave]
        except Exception:
            continue

    return fallback_spot


def convertir_a_usd(
    valor: float,
    fecha: Union[str, datetime.date, datetime.datetime, int],
    fx_mensual: dict[str, float],
    fx_spot: float,
) -> float:
    """
    Convierte `valor` en moneda local a USD.

    Usa el rate histórico del mes más cercano a `fecha`.
    Si no hay histórico disponible, usa fx_spot.
    value_usd = value_local / rate
    """
    rate = buscar_rate_por_fecha(fx_mensual, fecha, fallback_spot=fx_spot)
    if rate <= 0:
        return valor
    return valor / rate


def log_rates_diagnostico(moneda: str, ticker: str = "") -> None:
    """
    Loguea los rates crudos de spot e histórico para verificar dirección.
    Llamar antes de aplicar cualquier conversión.
    Solo útil para monedas no-USD.
    """
    if moneda.upper() == "USD":
        logger.info(f"[FX diag] {ticker or moneda}: USD — sin conversión.")
        return

    spot = obtener_fx_spot(moneda)
    hist = obtener_fx_historico(moneda)

    logger.info(
        f"[FX diag] {ticker or moneda} | moneda={moneda} | "
        f"spot={spot:.4f} {moneda}/USD | "
        f"histórico disponible: {len(hist)} meses"
    )

    if hist:
        fechas_sorted = sorted(hist.keys())
        for f in [fechas_sorted[0], fechas_sorted[len(fechas_sorted)//2], fechas_sorted[-1]]:
            logger.info(
                f"[FX diag]   {f}: {hist[f]:.4f} {moneda}/USD "
                f"→ 1 {moneda} = {1/hist[f]:.6f} USD"
            )

    logger.info(
        f"[FX diag] Verificación: 1 000 000 {moneda} "
        f"→ {1_000_000 / spot:,.2f} USD (spot)"
    )
