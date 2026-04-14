import math
import os
import requests

# Obtiene la tasa libre de riesgo desde la API de la Fed

_FRED_API_KEY_DEFAULT = "03b0d61b2efbea3313f92d4d117af8df"


def obtener_tasa_libre_riesgo():
    """Obtiene la tasa libre de riesgo desde la API de la Fed."""
    fred_api_key = os.environ.get("FRED_API_KEY", _FRED_API_KEY_DEFAULT)
    try:
        response = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "DGS10",
                "api_key": fred_api_key,
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
    if fcf_series is None:
        valores = []
    elif hasattr(fcf_series, "dropna"):
        valores = [float(v) for v in fcf_series.dropna().tolist()]
    else:
        valores = [float(v) for v in fcf_series if v is not None]

    def sanitize(valor, default=0.05):
        if valor is None:
            return default
        if isinstance(valor, complex):
            valor = valor.real
        try:
            valor = float(valor)
        except (TypeError, ValueError):
            return default
        if math.isnan(valor) or math.isinf(valor):
            return default
        return valor

    if len(valores) > 1:
        primero = valores[0]
        ultimo = valores[-1]
        cagr_calc = None
        if ultimo not in (0, None) and ultimo != 0 and primero > 0 and ultimo > 0:
            try:
                exponente = 1 / (len(valores) - 1)
                ratio = primero / ultimo
                if ratio > 0:
                    cagr_calc = (ratio ** exponente) - 1
            except (ZeroDivisionError, OverflowError, ValueError):
                cagr_calc = None

        valores_cronologicos = list(reversed(valores))
        tasas: list[float] = []
        for anterior, actual in zip(valores_cronologicos, valores_cronologicos[1:]):
            denominador = abs(anterior)
            if not denominador:
                continue
            variacion = (actual - anterior) / denominador
            tasas.append(variacion)

        promedio_calc = (sum(tasas) / len(tasas)) if tasas else None
        cagr = sanitize(cagr_calc)
        promedio = sanitize(promedio_calc)
    else:
        cagr = promedio = 0.05
    return cagr, promedio


def seleccionar_metodo_crecimiento(crecimiento_cagr, crecimiento_promedio):
    """Elige automáticamente la tasa más conservadora, la más cercana a cero."""

    opciones = [
        ("1", "CAGR", float(crecimiento_cagr)),
        ("2", "Promedio", float(crecimiento_promedio)),
    ]
    return min(
        opciones,
        key=lambda item: (abs(item[2]), 0 if item[0] == "1" else 1),
    )

# Calcula escenarios bull/base/bear


def calcular_escenarios(fcf_actual, crecimiento_base, wacc, debt, acciones, precio):
    """Genera tres escenarios (pesimista, base, optimista) con distintas tasas de crecimiento."""
    variaciones = {
        "bear": max(crecimiento_base * 0.4, crecimiento_base - 0.05),
        "base": crecimiento_base,
        "bull": min(crecimiento_base * 1.6, crecimiento_base + 0.08),
    }
    resultado = {}
    for nombre, tasa in variaciones.items():
        fcf_proj = proyectar_fcf(fcf_actual, tasa)
        valor_total = calcular_valor_intrinseco(fcf_proj, wacc)
        if valor_total is None or not acciones:
            resultado[nombre] = {
                "tasa_crecimiento_pct": round(tasa * 100, 1),
                "valor_intrinseco": None,
                "diferencia_pct": None,
                "estado": None,
            }
            continue
        equity_val = valor_total - debt
        valor_accion = equity_val / acciones
        diferencia_pct = ((valor_accion - precio) / precio * 100) if precio else None
        if valor_accion > precio * 1.1:
            estado = "SUBVALUADA"
        elif valor_accion < precio * 0.9:
            estado = "SOBREVALUADA"
        else:
            estado = "RAZONABLE"
        resultado[nombre] = {
            "tasa_crecimiento_pct": round(tasa * 100, 1),
            "valor_intrinseco": round(valor_accion, 2),
            "diferencia_pct": round(diferencia_pct, 1) if diferencia_pct is not None else None,
            "estado": estado,
        }
    return resultado


# Genera tabla de sensibilidad WACC × crecimiento


def calcular_tabla_sensibilidad(fcf_actual, wacc_base, crecimiento_base, debt, acciones, precio_actual):
    """
    Tabla 5×5: filas = variaciones de WACC, columnas = variaciones de crecimiento.
    Cada celda contiene el valor intrínseco por acción.
    """
    wacc_deltas = [-0.02, -0.01, 0.0, 0.01, 0.02]
    crec_deltas = [-0.04, -0.02, 0.0, 0.02, 0.04]

    waccs = [round(wacc_base + d, 4) for d in wacc_deltas]
    crecimientos = [round(crecimiento_base + d, 4) for d in crec_deltas]

    matrix = []
    for w in waccs:
        row = []
        for g in crecimientos:
            if w <= 0 or w <= g:
                row.append(None)
                continue
            fcf_proj = proyectar_fcf(fcf_actual, g)
            valor_total = calcular_valor_intrinseco(fcf_proj, w)
            if valor_total is None or not acciones:
                row.append(None)
                continue
            equity_val = valor_total - debt
            valor_accion = round(equity_val / acciones, 2)
            row.append(valor_accion)
        matrix.append(row)

    waccs_pct = [round(w * 100, 2) for w in waccs]
    crecimientos_pct = [round(g * 100, 2) for g in crecimientos]
    # Enrich rows with WACC label for easy template rendering
    rows = [
        {"wacc": w_pct, "cells": row_vals}
        for w_pct, row_vals in zip(waccs_pct, matrix)
    ]
    return {
        "waccs": waccs_pct,
        "crecimientos": crecimientos_pct,
        "matrix": matrix,
        "rows": rows,
        "precio_actual": precio_actual,
    }


# Calcula el valor intrínseco de la empresa usando FCF proyectado y valor residual


def calcular_valor_intrinseco(fcf_proyectado, wacc, crecimiento_perpetuo=0.02):
    """Calcula el valor intrínseco con FCF proyectado y valor residual."""
    if not fcf_proyectado or wacc <= 0:
        return None

    vp_fcf = sum(
        fcf / ((1 + wacc) ** i)
        for i, fcf in enumerate(fcf_proyectado, start=1)
    )

    crecimiento_ajustado = min(crecimiento_perpetuo, wacc - 0.005) if wacc > crecimiento_perpetuo else None
    if crecimiento_ajustado is None or crecimiento_ajustado < 0:
        return None

    fcf_final = fcf_proyectado[-1]
    try:
        valor_residual = (fcf_final * (1 + crecimiento_ajustado)) / (wacc - crecimiento_ajustado)
    except ZeroDivisionError:
        return None

    valor_residual_desc = valor_residual / ((1 + wacc) ** len(fcf_proyectado))
    return vp_fcf + valor_residual_desc
