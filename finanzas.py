import requests

# Obtiene la tasa libre de riesgo desde la API de la Fed


def obtener_tasa_libre_riesgo():
    """Obtiene la tasa libre de riesgo desde la API de la Fed."""
    try:
        response = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "DGS10",
                "api_key": "03b0d61b2efbea3313f92d4d117af8df",
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1
            }
        )
        response.raise_for_status()
        datos = response.json()
        ultima = next(
            (obs for obs in datos["observations"] if obs["value"] != "."), None)
        return float(ultima["value"]) / 100 if ultima else 0.0441
    except:
        return 0.0441

# Calcula el WACC (Weighted Average Cost of Capital)


def calcular_wacc(beta, debt, equity, cost_of_debt, tax_rate, risk_free_rate=0.0441, market_return=0.08):
    """Calcula el WACC con fórmula tradicional."""
    cost_of_equity = risk_free_rate + beta * (market_return - risk_free_rate)
    if equity + debt == 0:
        return 0
    return (equity / (equity + debt)) * cost_of_equity + (debt / (equity + debt)) * cost_of_debt * (1 - tax_rate)

# Proyecta el Free Cash Flow (FCF) a futuro


def proyectar_fcf(fcf_actual, tasa_crecimiento, años=5):
    """Proyecta el FCF a futuro respetando el comportamiento según si es positivo o negativo."""
    proyecciones = []
    for i in range(años):
        if i == 0:
            if fcf_actual > 0:
                fcf = fcf_actual * (1 + tasa_crecimiento)
            else:
                fcf = (-fcf_actual * tasa_crecimiento) + fcf_actual
        else:
            prev = proyecciones[-1]
            prev_prev = fcf_actual if i == 1 else proyecciones[-2]
            if prev > 0:
                fcf = prev * (1 + tasa_crecimiento)
            else:
                fcf = ((prev - prev_prev) * (1 + tasa_crecimiento)) + prev
        proyecciones.append(fcf)
    return proyecciones

# Calcular crecimiento de corto plazo:


def calcular_crecimientos(fcf_series):
    if len(fcf_series) > 1:
        cagr = (fcf_series.iloc[0] / fcf_series.iloc[-1]
                ) ** (1 / (len(fcf_series) - 1)) - 1
        tasas = [((fcf_series.iloc[i] / fcf_series.iloc[i+1]) - 1)
                 for i in range(len(fcf_series) - 1)]
        promedio = sum(tasas) / len(tasas)
    else:
        cagr = promedio = 0.05
    return float(cagr), float(promedio)

# Calcula el valor intrínseco de la empresa usando FCF proyectado y valor residual


def calcular_valor_intrinseco(fcf_proyectado, wacc, crecimiento_perpetuo=0.02):
    """Calcula el valor intrínseco con FCF proyectado y valor residual."""
    vp_fcf = sum(fcf / ((1 + wacc) ** i)
                 for i, fcf in enumerate(fcf_proyectado, start=1))
    fcf_final = fcf_proyectado[-1]
    valor_residual = (fcf_final * (1 + crecimiento_perpetuo)
                      ) / (wacc - crecimiento_perpetuo)
    valor_residual_desc = valor_residual / ((1 + wacc) ** len(fcf_proyectado))
    return vp_fcf + valor_residual_desc
