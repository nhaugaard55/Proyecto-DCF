import yfinance as yf
from finanzas import (
    obtener_tasa_libre_riesgo,
    calcular_wacc,
    proyectar_fcf,
    calcular_valor_intrinseco
)


def analizar_empresa(ticker, metodo_crecimiento="1", crecimiento=0.05, avg_growth_rate=0.05):
    empresa = yf.Ticker(ticker)
    info = empresa.info

    nombre = info.get("longName", ticker)
    sector = info.get("sector", "Desconocido")
    beta = info.get("beta", 1.0)
    tax_rate = info.get("effectiveTaxRate", 0.25)
    cost_of_debt = info.get("yield", 0.05)

    acciones = info.get("sharesOutstanding", 0)
    precio = empresa.history(period="1d")["Close"].iloc[-1]
    equity = acciones * precio

    balance = empresa.balance_sheet
    debt = balance.loc["Long Term Debt"][0] if "Long Term Debt" in balance.index else 0

    cashflow = empresa.cashflow
    fcf = cashflow.loc["Free Cash Flow"].dropna().head(5)
    fcf_actual = fcf.iloc[0]

    pe_ratio = info.get("trailingPE", "N/A")
    tasa_rf = obtener_tasa_libre_riesgo()
    market_return = 0.08

    print("\n📊 Datos:")
    print(f"🏷️ Nombre de la empresa: {nombre}")
    print(f"🏢 Sector: {sector}")
    print(f"📉 Price-to-Earnings Ratio (P/E): {pe_ratio}")
    print(f"🏦 Tasa libre de riesgo (Rf): {tasa_rf:.2%}")
    print(f"📊 Expected Market Return (Rm): {market_return:.2%}")
    print(f"💵 Precio actual: ${precio:.2f}")
    print(f"📈 Shares Outstanding: {acciones / 1_000_000_000:.4f}B")
    print(f"📐 Beta: {beta}")
    print(f"💰 Cost of Debt: {cost_of_debt:.2%}")
    print(f"🏦 Tax Rate: {tax_rate:.2%}")
    print(f"📊 Deuda (D): ${debt / 1_000_000_000:,.4f}B")
    print(f"💼 Market Cap (E): ${equity / 1_000_000_000:,.4f}B")

    # 🧪 Filtros clave del análisis:
    print("\n📊 Filtros clave del análisis:")

    # P/E Ratio
    try:
        pe_ratio_float = float(pe_ratio)
        print(
            f"P/E Ratio: {pe_ratio_float:.4f} {'❌' if pe_ratio_float > 20 else '✅'} (< 20)")
    except:
        print(f"P/E Ratio: {pe_ratio} ❌ (no disponible)")

    ps_ratio = precio / info.get("revenuePerShare",
                                 1) if info.get("revenuePerShare") else 0
    # P/S Ratio
    print(f"P/S Ratio: {ps_ratio:.2f} {'❌' if ps_ratio > 2 else '✅'} (< 2)")

    pb_ratio = precio / \
        info.get("bookValue", 1) if info.get("bookValue") else 0
    # P/B Ratio
    print(f"P/B Ratio: {pb_ratio:.2f} {'❌' if pb_ratio > 1 else '✅'} (< 1)")

    roe = info.get("returnOnEquity", 0)
    # ROE
    print(f"ROE: {roe:.2%} {'✅' if roe > 0.10 else '❌'} (> 10%)")

    debt_to_capital = debt / (debt + equity) if (debt + equity) else 0
    # Debt to Capital Ratio
    print(
        f"Debt to Capital Ratio: {debt_to_capital:.2%} {'✅' if debt_to_capital < 0.25 else '❌'} (< 25%)")

    volume = info.get("volume", 0)
    # Volume
    print(f"Volume: {volume:,} {'✅' if volume > 250000 else '❌'} (> 250,000)")

    revenue_growth = info.get("revenueGrowth", 0)
    # Revenue Growth
    print(
        f"Revenue Growth: {revenue_growth:.2%} {'✅' if revenue_growth > 0 else '❌'} (> 0%)")

    icr = info.get("ebitda", 0) / info.get("totalInterestExpense",
                                           1) if info.get("totalInterestExpense") else "N/A"
    # ICR (con control de tipo)
    try:
        icr_val = float(icr)
        icr_str = f"{icr_val:.2f} {'✅' if icr_val > 2 else '❌'}"
    except:
        icr_str = "N/A ❌"
    print(f"🧮 Interest Coverage Ratio (ICR): {icr_str}")

    # Calcular el crecimiento porcentual promedio (Average Growth Rate)
    # Se mueve fuera de esta función según indicación

    # Permitir al usuario elegir entre CAGR y crecimiento promedio
    if metodo_crecimiento == "2":
        tasa_crecimiento = avg_growth_rate
        print("📈 Usando crecimiento promedio.")
    else:
        tasa_crecimiento = crecimiento
        print("📈 Usando CAGR.")

    capm = tasa_rf + beta * (market_return - tasa_rf)
    wacc = calcular_wacc(beta, debt, equity, cost_of_debt, tax_rate, tasa_rf)

    print("\n📊 Cálculos:")
    print(f"💸 FCF actual: ${fcf_actual / 1_000_000_000:,.4f}B")
    print(f"📈 Tasa de crecimiento estimada (CAGR): {crecimiento:.2%}")
    print(
        f"📊 Tasa de crecimiento promedio (Average Growth Rate): {avg_growth_rate:.2%}")
    print(f"📉 Tasa de descuento (CAPM): {capm:.2%}")
    print(f"⚖️  WACC: {wacc:.2%}")
    capitalizacion = equity
    valor_empresa = equity + debt
    print(
        f"🏢 Capitalización de mercado: ${capitalizacion / 1_000_000_000:,.4f}B")
    print(
        f"🏷️ Valor de la compañía (Enterprise Value): ${valor_empresa / 1_000_000_000:,.4f}B")

    print("\n📈 Serie histórica de Free Cash Flow (FCF):")
    from datetime import datetime
    año_actual = datetime.now().year
    for i, val in enumerate(fcf.values):
        año = año_actual - i
        valor_billon = val / 1000000000
        print(f"{año}: ${valor_billon:,.4f}B")

    fcf_proy = proyectar_fcf(fcf_actual, tasa_crecimiento)

    print("\n📈 Proyección de FCF para los próximos 5 años:")
    año_inicio_proy = año_actual + 1
    for i, fcf_ano in enumerate(fcf_proy):
        año_proy = año_inicio_proy + i
        print(f"{año_proy}: ${fcf_ano / 1_000_000_000:,.4f}B")

    crecimiento_largo_plazo = 0.02
    fcf_final = fcf_proy[-1]
    valor_terminal = (fcf_final * (1 + crecimiento_largo_plazo)
                      ) / (wacc - crecimiento_largo_plazo)

    print("\n📉 Supuestos a Largo Plazo:")
    print(f"📈 Expected Long-Term Growth: {crecimiento_largo_plazo:.2%}")
    print(
        f"🏁 Valor terminal (Terminal Value): ${valor_terminal / 1_000_000_000:,.4f}B")

    # Mostrar el valor presente de los FCF proyectados año por año
    print("\n💵 Valor presente de los FCF proyectados por año:")
    vp_fcf_total = 0
    for i, fcf in enumerate(fcf_proy, start=1):
        vp = fcf / ((1 + wacc) ** i)
        vp_fcf_total += vp
        print(f"Año {i}: PV = ${vp / 1_000_000_000:,.4f}B")

    print(
        f"\n💰 Valor presente total (PV) de los FCF: ${vp_fcf_total / 1_000_000_000:,.4f}B")

    valor_residual_desc = valor_terminal / ((1 + wacc) ** len(fcf_proy))
    print(
        f"💵 Valor residual descontado (Present Value of Terminal Value): ${valor_residual_desc / 1_000_000_000:,.4f}B")

    valor_total = calcular_valor_intrinseco(fcf_proy, wacc)
    equity_value = valor_total - debt
    valor_por_accion = equity_value / acciones if acciones else 0

    diferencia_pct = ((valor_por_accion - precio) / precio) * 100

    valor_intrinseco = valor_por_accion
    current_price = precio

    # Nuevos datos añadidos después de los filtros existentes:

    # 📈 Dividend Yield
    dividend_yield = info.get("dividendYield")
    if dividend_yield is not None:
        print(
            f"📈 Dividend Yield: {dividend_yield * 100:.2f}% {'✅' if dividend_yield > 0.02 else '❌'} (> 2%)")
    else:
        print("📈 Dividend Yield: No disponible")

    # 🚀 Dividend Growth Rate (requiere cálculo manual o fuente externa)
    print("🚀 Dividend Growth Rate: No disponible (requiere histórico)")

    # 📅 Años pagando dividendos (manual/histórico)
    print("📅 Años pagando dividendos: No disponible (requiere histórico)")

    # 🧮 Net Worth / Shares
    total_assets = info.get("totalAssets")
    total_liabilities = info.get("totalLiab")
    shares_outstanding = info.get("sharesOutstanding")
    if total_assets and total_liabilities and shares_outstanding:
        net_worth_per_share = (
            total_assets - total_liabilities) / shares_outstanding
        print(f"🧮 Net Worth / Share: ${net_worth_per_share:.2f}")
    else:
        print("🧮 Net Worth / Share: No disponible")

    # 🧠 Intrinsic Value (ya lo estás calculando, solo lo mostramos)
    if valor_intrinseco is not None:
        print(f"🧠 Intrinsic Value: ${valor_intrinseco:.2f}")
    else:
        print("🧠 Intrinsic Value: No disponible")

    # 🛡️ Safety Margin
    if valor_intrinseco and current_price:
        safety_margin = (valor_intrinseco - current_price) / valor_intrinseco
        print(
            f"🛡️ Safety Margin: {safety_margin:.2%} {'✅' if safety_margin > 0.25 else '❌'} (> 25%)")
    else:
        print("🛡️ Safety Margin: No disponible")

    # 📉 52-Week Low
    week_52_low = info.get("fiftyTwoWeekLow")
    if week_52_low:
        print(f"📉 52-week low: ${week_52_low}")
    else:
        print("📉 52-week low: No disponible")

    # 📊 Bass pattern (visual)
    print("📊 Bass pattern (manual): Observación técnica recomendada")

    return {
        "nombre": nombre,
        "sector": sector,
        "valor_intrinseco": valor_por_accion,
        "precio_actual": precio,
        "diferencia": valor_por_accion - precio,
        "diferencia_pct": diferencia_pct,
        "estado": "SUBVALUADA" if valor_por_accion > precio * 1.1 else "SOBREVALUADA" if valor_por_accion < precio * 0.9 else "RAZONABLE"
    }
