import re
from datetime import datetime
from typing import Dict, Optional, Sequence

import yfinance as yf

from .ai_summary import AISummaryError, generar_resumen_sentimiento
from .finanzas import (
    obtener_tasa_libre_riesgo,
    calcular_wacc,
    proyectar_fcf,
    calcular_valor_intrinseco
)

from .finnhub import FinnhubError, obtener_noticias_finnhub
from .fmp import FCFEntry, FMPClientError, obtener_noticias_empresa as obtener_noticias_fmp

MAX_NEWS_ITEMS = 18


def analizar_empresa(
    ticker,
    metodo_crecimiento="1",
    crecimiento=0.05,
    avg_growth_rate=0.05,
    fcf_historial: Optional[Sequence[FCFEntry]] = None,
    tax_rate_override: Optional[float] = None,
    cost_of_debt_override: Optional[float] = None,
    metricas_fuente: Optional[Dict[str, dict]] = None,
):
    empresa = yf.Ticker(ticker)
    info = getattr(empresa, "info", {}) or {}
    history = empresa.history(period="1d")

    def to_float(value, default=0.0):
        if isinstance(value, complex):
            value = value.real
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def to_billions(value):
        if isinstance(value, complex):
            value = value.real
        try:
            return float(value) / 1_000_000_000 if value is not None else None
        except (TypeError, ValueError):
            return None

    nombre = info.get("longName", ticker)
    sector = info.get("sector", "Desconocido")
    beta = to_float(info.get("beta"), 1.0)
    tax_rate_info = to_float(info.get("effectiveTaxRate"), 0.25)
    cost_of_debt_info = to_float(info.get("yield"), 0.05)

    tax_rate = tax_rate_info if tax_rate_override is None else float(tax_rate_override)
    cost_of_debt = cost_of_debt_info if cost_of_debt_override is None else float(cost_of_debt_override)

    acciones = to_float(info.get("sharesOutstanding"), 0)
    precio = 0.0
    if not history.empty:
        precio = to_float(history["Close"].iloc[-1], 0)
    else:
        precio = to_float(info.get("currentPrice")
                          or info.get("previousClose"), 0)

    equity = acciones * precio

    balance = getattr(empresa, "balance_sheet", None)
    debt = 0.0
    if balance is not None and not balance.empty and "Long Term Debt" in balance.index:
        deuda_series = balance.loc["Long Term Debt"].dropna()
        if not deuda_series.empty:
            debt = to_float(deuda_series.iloc[0], 0)

    fcf: list[float] = []
    fcf_presentacion: list[tuple[Optional[int], float]] = []
    if fcf_historial:
        for entrada in fcf_historial:
            raw_valor = getattr(entrada, "value", None)
            if raw_valor is None:
                continue
            valor = to_float(raw_valor, 0.0)
            fcf.append(valor)

            raw_year = getattr(entrada, "year", None)
            try:
                year = int(raw_year) if raw_year is not None else None
            except (TypeError, ValueError):
                year = None
            fcf_presentacion.append((year, valor))
    else:
        cashflow = getattr(empresa, "cashflow", None)
        if cashflow is not None and not cashflow.empty and "Free Cash Flow" in cashflow.index:
            fcf_series = cashflow.loc["Free Cash Flow"].dropna().head(5)
            if hasattr(fcf_series, "tolist"):
                fcf = [to_float(valor) for valor in fcf_series.tolist()]
            elif isinstance(fcf_series, (list, tuple)):
                fcf = [to_float(valor) for valor in fcf_series]
        for valor in fcf:
            fcf_presentacion.append((None, valor))

    fcf_actual = fcf[0] if fcf else 0.0

    pe_ratio_raw = info.get("trailingPE")
    pe_ratio = None

    pe_ratio_raw = info.get("trailingPE")
    pe_ratio = to_float(pe_ratio_raw) if pe_ratio_raw is not None else None

    tasa_rf = obtener_tasa_libre_riesgo()
    market_return = 0.08

    if metodo_crecimiento == "2":
        tasa_crecimiento = avg_growth_rate
        metodo_utilizado = "Promedio"
    else:
        tasa_crecimiento = crecimiento
        metodo_utilizado = "CAGR"

    capm = tasa_rf + beta * (market_return - tasa_rf)
    wacc = calcular_wacc(beta, debt, equity, cost_of_debt, tax_rate, tasa_rf)

    revenue_per_share_raw = info.get("revenuePerShare")
    revenue_per_share = to_float(
        revenue_per_share_raw) if revenue_per_share_raw else None
    book_value_raw = info.get("bookValue")
    book_value = to_float(book_value_raw) if book_value_raw else None

    ps_ratio = (precio / revenue_per_share) if revenue_per_share else None
    pb_ratio = (precio / book_value) if book_value else None
    roe = info.get("returnOnEquity")
    debt_to_capital = (debt / (debt + equity)) if (debt + equity) else 0
    volume = to_float(info.get("volume"), 0)
    revenue_growth = info.get("revenueGrowth")
    icr = None
    if info.get("totalInterestExpense"):
        try:
            icr = info.get("ebitda", 0) / info.get("totalInterestExpense")
        except (TypeError, ZeroDivisionError):
            icr = None

    filtros = []
    filtros.append({
        "nombre": "P/E",
        "valor": f"{pe_ratio:.2f}" if pe_ratio is not None else "N/D",
        "criterio": "< 20",
        "cumple": pe_ratio is not None and pe_ratio <= 20
    })
    filtros.append({
        "nombre": "P/S",
        "valor": f"{ps_ratio:.2f}" if ps_ratio is not None else "N/D",
        "criterio": "< 2",
        "cumple": ps_ratio is not None and ps_ratio <= 2
    })
    filtros.append({
        "nombre": "P/B",
        "valor": f"{pb_ratio:.2f}" if pb_ratio is not None else "N/D",
        "criterio": "< 1",
        "cumple": pb_ratio is not None and pb_ratio <= 1
    })
    filtros.append({
        "nombre": "ROE",
        "valor": f"{roe:.2%}" if isinstance(roe, (int, float)) else "N/D",
        "criterio": "> 10%",
        "cumple": isinstance(roe, (int, float)) and roe > 0.10
    })
    filtros.append({
        "nombre": "Debt/Capital",
        "valor": f"{debt_to_capital:.2%}",
        "criterio": "< 25%",
        "cumple": debt_to_capital < 0.25
    })
    filtros.append({
        "nombre": "Volumen",
        "valor": f"{volume:,.0f}" if volume else "N/D",
        "criterio": "> 250k",
        "cumple": volume and volume > 250000
    })
    filtros.append({
        "nombre": "Revenue Growth",
        "valor": f"{revenue_growth:.2%}" if isinstance(revenue_growth, (int, float)) else "N/D",
        "criterio": "> 0%",
        "cumple": isinstance(revenue_growth, (int, float)) and revenue_growth > 0
    })
    filtros.append({
        "nombre": "ICR",
        "valor": f"{icr:.2f}" if isinstance(icr, (int, float)) else "N/D",
        "criterio": "> 2",
        "cumple": isinstance(icr, (int, float)) and icr > 2
    })

    año_actual = datetime.now().year
    fcf_historico = []
    for indice, (year, valor) in enumerate(fcf_presentacion[:7]):
        anio = year if year is not None else año_actual - indice
        fcf_historico.append({
            "anio": anio,
            "valor": to_billions(valor)
        })

    fcf_proyectado = proyectar_fcf(fcf_actual, tasa_crecimiento)
    fcf_proyecciones = []
    for i, valor in enumerate(fcf_proyectado, start=1):
        fcf_proyecciones.append({
            "anio": año_actual + i,
            "valor": to_billions(valor)
        })

    crecimiento_largo_plazo = 0.02
    valor_total = calcular_valor_intrinseco(fcf_proyectado, wacc)
    equity_value = (valor_total - debt) if valor_total is not None else None
    valor_por_accion = None
    if equity_value is not None and acciones:
        valor_por_accion = equity_value / acciones

    diferencia = None
    if valor_por_accion is not None:
        diferencia = valor_por_accion - precio

    diferencia_pct = None
    if diferencia is not None and precio:
        diferencia_pct = (diferencia / precio) * 100

    def to_optional_float(value):
        try:
            if isinstance(value, complex):
                value = value.real
            return float(value)
        except (TypeError, ValueError):
            return None

    def normalizar_dividend_yield(raw_yield, raw_dividend_rate, current_price):
        valor = to_optional_float(raw_yield)
        if valor is not None:
            if valor > 5:  # algunos proveedores devuelven el porcentaje sin dividir por 100
                valor = valor / 100.0
            if valor < 0:
                valor = None

        tasa = to_optional_float(raw_dividend_rate)
        precio_actual = to_optional_float(current_price)
        calculado = None
        if tasa is not None and precio_actual not in (None, 0):
            try:
                calculado = max(tasa / precio_actual, 0.0)
            except ZeroDivisionError:
                calculado = None

        if valor is None:
            return calculado

        if calculado is not None:
            # Si el valor difiere demasiado del cálculo con dividendRate, preferimos el calculado.
            limite_base = 0.1  # 10%
            limite_superior = max(calculado * 4, limite_base)
            if valor > limite_superior:
                return calculado

        return valor

    dividend_rate = info.get("dividendRate")
    dividend_yield = normalizar_dividend_yield(info.get("dividendYield"), dividend_rate, precio)
    total_assets = info.get("totalAssets")
    total_liabilities = info.get("totalLiab")
    net_worth_per_share = None
    if total_assets and total_liabilities and acciones:
        net_worth_per_share = (total_assets - total_liabilities) / acciones

    safety_margin = None
    if valor_por_accion is not None and precio:
        try:
            safety_margin = (valor_por_accion - precio) / valor_por_accion
        except ZeroDivisionError:
            safety_margin = None

    filtros.append({
        "nombre": "Safety Margin",
        "valor": f"{safety_margin:.2%}" if isinstance(safety_margin, (int, float)) else "N/D",
        "criterio": "> 0%",
        "cumple": isinstance(safety_margin, (int, float)) and safety_margin > 0
    })

    valor_terminal = None
    if valor_total is not None:
        fcf_final = fcf_proyectado[-1] if fcf_proyectado else 0
        if wacc > 0 and crecimiento_largo_plazo < wacc:
            valor_terminal = (
                fcf_final * (1 + crecimiento_largo_plazo)) / (wacc - crecimiento_largo_plazo)

    valor_terminal_billones = to_billions(valor_terminal)

    datos_empresa = {
        "nombre": nombre,
        "sector": sector,
        "precio_actual": precio,
        "acciones": acciones,
        "acciones_billones": to_billions(acciones),
        "market_cap": equity,
        "market_cap_billones": to_billions(equity),
        "deuda": debt,
        "deuda_billones": to_billions(debt),
        "beta": beta,
        "tasa_impositiva": tax_rate,
        "tasa_impositiva_pct": tax_rate * 100 if tax_rate is not None else None,
        "cost_of_debt": cost_of_debt,
        "cost_of_debt_pct": cost_of_debt * 100 if cost_of_debt is not None else None,
        "metodo_crecimiento": metodo_utilizado,
    }

    detalles_metricas = metricas_fuente or {}
    datos_empresa["tasa_impositiva_fuente"] = detalles_metricas.get("tax_rate", {}).get("descripcion")
    datos_empresa["tasa_impositiva_anios"] = detalles_metricas.get("tax_rate", {}).get("años")
    datos_empresa["cost_of_debt_fuente"] = detalles_metricas.get("cost_of_debt", {}).get("descripcion")
    datos_empresa["cost_of_debt_anios"] = detalles_metricas.get("cost_of_debt", {}).get("años")

    metricas = {
        "tasa_rf": tasa_rf,
        "tasa_rf_pct": tasa_rf * 100 if tasa_rf is not None else None,
        "market_return": market_return,
        "market_return_pct": market_return * 100 if market_return is not None else None,
        "capm": capm,
        "capm_pct": capm * 100 if capm is not None else None,
        "wacc": wacc,
        "wacc_pct": wacc * 100 if wacc is not None else None,
        "crecimiento": tasa_crecimiento,
        "crecimiento_pct": tasa_crecimiento * 100 if tasa_crecimiento is not None else None,
        "crecimiento_cagr": crecimiento,
        "crecimiento_cagr_pct": crecimiento * 100 if crecimiento is not None else None,
        "crecimiento_promedio": avg_growth_rate,
        "crecimiento_promedio_pct": avg_growth_rate * 100 if avg_growth_rate is not None else None,
        "valor_terminal": valor_terminal_billones,
        "detalles_fuente": detalles_metricas,
    }

    dividendos = {
        "yield": dividend_yield,
        "yield_pct": dividend_yield * 100 if dividend_yield is not None else None,
        "net_worth_per_share": net_worth_per_share,
        "safety_margin": safety_margin,
        "safety_margin_pct": safety_margin * 100 if safety_margin is not None else None,
        "fifty_two_week_low": info.get("fiftyTwoWeekLow")
    }

    noticias: list[dict] = []
    noticias_fuentes: set[str] = set()
    noticias_error: Optional[str] = None

    yfinance_consultado = False

    try:
        raw_news_any = getattr(empresa, "news", None)
    except Exception as exc:  # pragma: no cover - dependiente de la red
        raw_news: list = []
        noticias_error = f"YFinance no devolvió noticias ({exc})"
    else:
        raw_news = list(raw_news_any or [])
    finally:
        yfinance_consultado = True

    if raw_news:
        noticias_fuentes.add("yfinance")
        for item in raw_news[:MAX_NEWS_ITEMS]:
            if not isinstance(item, dict):
                continue
            titulo = (item.get("title") or item.get("headline") or "").strip()
            enlace = (item.get("link") or item.get("url") or "").strip()
            if not titulo or not enlace:
                continue

            fuente = (item.get("publisher") or item.get("source") or "").strip() or None
            resumen = (item.get("summary") or item.get("content") or item.get("description") or None)
            if resumen:
                resumen = resumen.strip() or None

            imagen = None
            thumbnail = item.get("thumbnail")
            if isinstance(thumbnail, dict):
                url_directa = thumbnail.get("url")
                if isinstance(url_directa, str) and url_directa.strip():
                    imagen = url_directa.strip()
                else:
                    resoluciones = thumbnail.get("resolutions")
                    if isinstance(resoluciones, list):
                        for res in resoluciones:
                            url_res = (res.get("url") if isinstance(res, dict) else None)
                            if isinstance(url_res, str) and url_res.strip():
                                imagen = url_res.strip()
                                break

            publicado = None
            marca_tiempo = item.get("providerPublishTime") or item.get("providerPublishTimeUTC") or item.get("datetime")
            if isinstance(marca_tiempo, (int, float)):
                try:
                    publicado = datetime.fromtimestamp(marca_tiempo)
                except (OSError, ValueError, OverflowError):
                    publicado = None

            noticias.append({
                "titulo": titulo,
                "fuente": fuente,
                "resumen": resumen,
                "url": enlace,
                "imagen": imagen,
                "fecha": publicado,
            })

    if len(noticias) < MAX_NEWS_ITEMS:
        if not noticias and yfinance_consultado and "yfinance" not in noticias_fuentes and noticias_error is None:
            noticias_error = "YFinance no reportó noticias recientes para este ticker."

        restante = MAX_NEWS_ITEMS - len(noticias)
        if restante > 0:
            try:
                noticias_fmp = obtener_noticias_fmp(ticker, limite=restante)
            except FMPClientError as exc:
                mensaje = _limpiar_mensaje_api(str(exc))
                noticias_error = f"{noticias_error}. {mensaje}" if noticias_error else mensaje
            except Exception as exc:  # pragma: no cover - dependiente de la red
                mensaje = f"No se pudieron obtener noticias desde Financial Modeling Prep ({_limpiar_mensaje_api(str(exc))})."
                noticias_error = f"{noticias_error}. {mensaje}" if noticias_error else mensaje
            else:
                if noticias_fmp:
                    noticias_fuentes.add("fmp")
                    for noticia in noticias_fmp:
                        noticias.append(
                            {
                                "titulo": noticia.title,
                                "fuente": noticia.site,
                                "resumen": noticia.summary,
                                "url": noticia.url,
                                "imagen": noticia.image,
                                "fecha": noticia.published_at,
                            }
                        )

        if len(noticias) < MAX_NEWS_ITEMS:
            restante = MAX_NEWS_ITEMS - len(noticias)
            if restante > 0:
                try:
                    noticias_finnhub = obtener_noticias_finnhub(ticker, limite=restante)
                except FinnhubError as exc:
                    mensaje = _limpiar_mensaje_api(str(exc))
                    noticias_error = f"{noticias_error}. {mensaje}" if noticias_error else mensaje
                except Exception as exc:  # pragma: no cover - dependiente de la red
                    mensaje = f"No se pudieron obtener noticias desde Finnhub ({_limpiar_mensaje_api(str(exc))})."
                    noticias_error = f"{noticias_error}. {mensaje}" if noticias_error else mensaje
                else:
                    if noticias_finnhub:
                        noticias_fuentes.add("finnhub")
                        for noticia in noticias_finnhub:
                            noticias.append(
                                {
                                    "titulo": noticia.title,
                                    "fuente": noticia.source,
                                    "resumen": noticia.summary,
                                    "url": noticia.url,
                                    "imagen": noticia.image,
                                    "fecha": noticia.published_at,
                                }
                            )

    noticias_por_url: dict[str, dict] = {}
    for item in noticias:
        url = item.get("url")
        if not url:
            continue
        existente = noticias_por_url.get(url)
        fecha_item = item.get("fecha")
        fecha_existente = existente.get("fecha") if existente else None
        if existente is None or (
            isinstance(fecha_item, datetime)
            and (not isinstance(fecha_existente, datetime) or fecha_item > fecha_existente)
        ):
            noticias_por_url[url] = item

    noticias = list(noticias_por_url.values())
    noticias.sort(
        key=lambda n: (
            n.get("fecha") is None,
            -(n.get("fecha").timestamp()) if isinstance(n.get("fecha"), datetime) else 0,
        )
    )
    noticias = noticias[:MAX_NEWS_ITEMS]

    if not noticias and noticias_error is None:
        noticias_error = "No se encontraron noticias recientes para este ticker."

    mapa_fuentes = {
        "yfinance": "YFinance",
        "fmp": "Financial Modeling Prep",
        "finnhub": "Finnhub",
    }
    fuentes_detectadas = [mapa_fuentes.get(f, f.title()) for f in sorted(noticias_fuentes)]
    noticias_fuente_descripcion = ", ".join(fuentes_detectadas) if fuentes_detectadas else None

    resumen_noticias = None
    resumen_noticias_error = None

    ticker_lower = ticker.lower()
    nombre_normalizado = nombre.lower()
    nombre_sin_inc = nombre_normalizado.replace(" inc.", "").replace(" inc", "")
    nombre_simple = nombre.split(" ")[0].lower() if nombre else ""

    def _menciona(texto: Optional[str]) -> bool:
        texto_busqueda = str(texto or "").lower()
        if not texto_busqueda.strip():
            return False
        if ticker_lower and ticker_lower in texto_busqueda:
            return True
        if nombre_normalizado and nombre_normalizado in texto_busqueda:
            return True
        if nombre_sin_inc and nombre_sin_inc.strip() and nombre_sin_inc in texto_busqueda:
            return True
        if nombre_simple:
            patron = fr"\b{re.escape(nombre_simple)}['’]s\b"
            if re.search(patron, texto_busqueda):
                return True
        return False

    noticias_resumen: list[dict] = []
    if noticias:
        for item in noticias:
            copia = dict(item)
            copia["empresa"] = nombre
            noticias_resumen.append(copia)

        relevantes_titulo = [n for n in noticias_resumen if _menciona(n.get("titulo"))]
        if relevantes_titulo:
            noticias_resumen = relevantes_titulo
        else:
            relevantes_contenido = [
                n for n in noticias_resumen if _menciona(n.get("titulo")) or _menciona(n.get("resumen"))
            ]
            if relevantes_contenido:
                noticias_resumen = relevantes_contenido

    if noticias_resumen:
        try:
            resumen_noticias = generar_resumen_sentimiento(noticias_resumen)
        except AISummaryError as exc:
            resumen_noticias_error = _limpiar_mensaje_api(str(exc))
        except Exception as exc:  # pragma: no cover
            resumen_noticias_error = f"Error generando el resumen con IA ({_limpiar_mensaje_api(str(exc))})."

    estado = None
    if valor_por_accion is not None and precio:
        if valor_por_accion > precio * 1.1:
            estado = "SUBVALUADA"
        elif valor_por_accion < precio * 0.9:
            estado = "SOBREVALUADA"
        else:
            estado = "RAZONABLE"

    return {
        "nombre": nombre,
        "sector": sector,
        "valor_intrinseco": valor_por_accion,
        "precio_actual": precio,
        "diferencia": diferencia,
        "diferencia_pct": diferencia_pct,
        "estado": estado,
        "datos_empresa": datos_empresa,
        "filtros": filtros,
        "metricas": metricas,
        "fcf_historico": fcf_historico,
        "fcf_proyectado": fcf_proyecciones,
        "dividendos": dividendos,
        "metricas_fuente": detalles_metricas,
        "noticias": noticias,
        "noticias_fuente": ",".join(sorted(noticias_fuentes)) if noticias_fuentes else None,
        "noticias_error": noticias_error,
        "noticias_fuente_descripcion": noticias_fuente_descripcion,
        "resumen_noticias": resumen_noticias,
        "resumen_noticias_error": resumen_noticias_error,
    }


def _limpiar_mensaje_api(texto: str) -> str:
    """Oculta parámetros sensibles (como claves) dentro de mensajes de error."""

    if not texto:
        return texto

    # Reemplaza apikey=XXXX por apikey=****
    texto = re.sub(r"apikey=[^&\s]+", "apikey=****", texto)
    texto = re.sub(r"token=[^&\s]+", "token=****", texto)
    return texto
