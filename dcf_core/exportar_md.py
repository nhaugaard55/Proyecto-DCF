"""
Exportador Markdown — uso exclusivo admin/QA.

Genera un snapshot determinista del análisis completo. El timestamp queda
en la primera línea (fácil de ignorar en un diff); el resto del contenido
es estable para el mismo ticker sin cambios de código.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


# ── helpers ────────────────────────────────────────────────────────────────────

def _fmt(v: Any, decimals: int = 2, prefix: str = "", suffix: str = "", fallback: str = "N/D") -> str:
    if v is None or v == "":
        return fallback
    try:
        f = float(v)
        return f"{prefix}{f:,.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _pct(v: Any, decimals: int = 2, signed: bool = False, fallback: str = "N/D") -> str:
    """Formats a value already in percentage (e.g. 7.3 → '7.30%')."""
    if v is None or v == "":
        return fallback
    try:
        f = float(v)
        sign = "+" if signed and f > 0 else ""
        return f"{sign}{f:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(v)


def _pct_dec(v: Any, decimals: int = 2, signed: bool = False, fallback: str = "N/D") -> str:
    """Formats a decimal fraction as percentage (e.g. 0.073 → '7.30%')."""
    if v is None or v == "":
        return fallback
    try:
        f = float(v) * 100
        sign = "+" if signed and f > 0 else ""
        return f"{sign}{f:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(v)


def _money(v: Any, fallback: str = "N/D") -> str:
    if v is None or v == "":
        return fallback
    try:
        f = float(v)
        sign = "-" if f < 0 else ""
        a = abs(f)
        if a >= 1e12:
            return f"{sign}${a/1e12:.2f}T"
        if a >= 1e9:
            return f"{sign}${a/1e9:.2f}B"
        if a >= 1e6:
            return f"{sign}${a/1e6:.2f}M"
        if a >= 1e3:
            return f"{sign}${a/1e3:.1f}k"
        return f"{sign}${a:.2f}"
    except (TypeError, ValueError):
        return str(v)


def _price(v: Any, fallback: str = "N/D") -> str:
    if v is None or v == "":
        return fallback
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _s(v: Any, fallback: str = "N/D") -> str:
    if v is None or v == "":
        return fallback
    return str(v).strip() or fallback


def _section(title: str, level: int = 2) -> str:
    return f"\n{'#' * level} {title}\n"


def _row(label: str, value: str, width: int = 35) -> str:
    pad = " " * max(0, width - len(label))
    return f"| {label}{pad}| {value} |"


def _table_header(cols: list[str]) -> str:
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    return header + "\n" + sep


# ── main builder ───────────────────────────────────────────────────────────────

def build_admin_md(
    ticker: str,
    resultado: dict,
    multi_model: Optional[dict],
    company_stage: Optional[dict],
    analyst_data: Optional[dict] = None,
    insider_data: Optional[dict] = None,
) -> str:
    now = datetime.now()
    lines: list[str] = []

    def ln(text: str = "") -> None:
        lines.append(text)

    # ── HEADER (timestamp isolated on line 1) ──────────────────────────────────
    ln(f"<!-- generated: {now.strftime('%Y-%m-%dT%H:%M:%S')} -->")
    ln(f"# Intrinsic — Análisis completo: {ticker}")
    ln()
    ln(f"**Ticker:** {ticker}  ")
    ln(f"**Fecha análisis:** {now.strftime('%Y-%m-%d %H:%M:%S')}  ")

    datos = resultado.get("datos_empresa") or {}
    metricas = resultado.get("metricas") or {}

    nombre = _s(resultado.get("nombre") or datos.get("nombre") or ticker)
    sector = _s(resultado.get("sector") or datos.get("sector"))
    exchange = _s(datos.get("exchange") or resultado.get("exchange"))
    fuente = _s(resultado.get("fuente_datos"))

    moneda_reporte = datos.get("moneda_reporte") or "USD"
    fx_aplicado = datos.get("fx_aplicado")
    fx_label = f"{fx_aplicado:,.2f} {moneda_reporte}/USD" if fx_aplicado is not None else "N/A (USD)"

    ln(f"**Empresa:** {nombre}  ")
    ln(f"**Sector:** {sector}  ")
    ln(f"**Exchange:** {exchange}  ")
    ln(f"**Fuente datos FCF:** {fuente}  ")
    ln(f"**Moneda reporte:** {moneda_reporte}  ")
    ln(f"**FX aplicado:** {fx_label}  ")
    ln()

    # ── 1. PRECIO Y VEREDICTO ─────────────────────────────────────────────────
    ln(_section("1. Precio y Veredicto"))

    precio_actual = resultado.get("precio_actual")
    ln(f"| {'Campo':<35}| Valor |")
    ln(f"| {'':-<35}| ----- |")
    ln(_row("Precio actual", _price(precio_actual)))

    consenso = (multi_model or {}).get("consenso") or {}
    valor_consenso = consenso.get("precio")
    upside_consenso = consenso.get("upside_pct")
    veredicto = consenso.get("veredicto") or resultado.get("estado")

    ln(_row("Valor intrínseco ponderado (consenso)", _price(valor_consenso)))
    ln(_row("Margen de seguridad / Upside estimado", _pct(upside_consenso, signed=True)))
    ln(_row("Veredicto", _s(veredicto)))
    ln(_row("Confianza del consenso", _s(consenso.get("confianza"))))
    ln(_row("Dispersión entre modelos (DR)", _pct(consenso.get("disagreement_ratio_pct"))))
    ln(_row("Modelos usados en consenso", str(consenso.get("modelos_usados", "N/D"))))
    ln(_row("Estado análisis DCF simple", _s(resultado.get("estado"))))
    ln(_row("Valor intrínseco DCF simple", _price(resultado.get("valor_intrinseco"))))
    ln(_row("Diferencia DCF simple vs precio", _pct(resultado.get("diferencia_pct"), signed=True)))
    ln()

    # Score final
    score_final = (multi_model or {}).get("score_final") or {}
    if score_final:
        ln(_section("1.1 Score Final", level=3))
        ln(f"| {'Campo':<35}| Valor |")
        ln(f"| {'':-<35}| ----- |")
        ln(_row("Score", _fmt(score_final.get("score"), decimals=1, suffix="/10")))
        ln(_row("Recomendación", _s(score_final.get("recomendacion"))))
        ln(_row("Nota etapa", _s(score_final.get("nota_etapa"), fallback="")))
        ln(_row("Nota consenso", _s(score_final.get("nota_consenso"), fallback="")))
        ln(_row("Advertencia", _s(score_final.get("advertencia"), fallback="")))
        ln()
        componentes = score_final.get("componentes") or {}
        ln(_section("Componentes del score", level=4))
        ln(_table_header(["Componente", "Puntos", "Peso", "Detalle"]))
        for key in ("upside", "confianza", "solvencia", "fundamentals"):
            c = componentes.get(key) or {}
            ln(f"| {key} | {_fmt(c.get('puntos'), decimals=1)} | {_pct_dec(c.get('peso'), decimals=0)} | {_s(c.get('detalle'), fallback='')} |")
        ln()

    # ── 2. CICLO DE VIDA (COMPANY STAGE) ─────────────────────────────────────
    ln(_section("2. Etapa del Ciclo de Vida"))
    cs = company_stage or {}
    ln(f"| {'Campo':<35}| Valor |")
    ln(f"| {'':-<35}| ----- |")
    ln(_row("Stage número", str(cs.get("stage", "N/D"))))
    ln(_row("Nombre etapa", _s(cs.get("nombre") or cs.get("stage_name"))))
    ln(_row("Descripción breve", _s(cs.get("descripcion_breve"), fallback="")))
    criterio = cs.get("criterio") or cs.get("criterios_activos") or cs.get("razon")
    ln(_row("Criterio de detección", _s(criterio, fallback="")))
    ln()

    # Stage context desde multi_model
    stage_context = (multi_model or {}).get("stage_context") or {}
    if stage_context:
        ln(_section("2.1 Contexto de Etapa (multi_model)", level=3))
        ln(f"- Modelos útiles: {', '.join(stage_context.get('modelos_utiles') or []) or 'N/D'}")
        ln(f"- Modelos algo útiles: {', '.join(stage_context.get('modelos_algo_utiles') or []) or 'N/D'}")
        ln(f"- Modelos no útiles: {', '.join(stage_context.get('modelos_no_utiles') or []) or 'N/D'}")
        nota_esp = stage_context.get("nota_especial")
        if nota_esp:
            ln(f"- **Nota especial:** {nota_esp}")
        ln()

    # Filtros de etapa
    filtros = resultado.get("filtros") or []
    if filtros:
        ln(_section("2.2 Filtros Fundamentales de Etapa", level=3))
        ln(_table_header(["Filtro", "Valor", "Criterio", "Cumple", "Relevancia"]))
        for f in filtros:
            if not isinstance(f, dict):
                continue
            ln(f"| {_s(f.get('nombre'))} | {_s(f.get('valor_display') or f.get('valor'))} | {_s(f.get('criterio'), fallback='')} | {'✓' if f.get('cumple') else '✗'} | {_s(f.get('relevancia'), fallback='')} |")
        ln()

    # ── 3. WACC Y COMPONENTES ─────────────────────────────────────────────────
    ln(_section("3. WACC y Componentes"))
    ln(f"| {'Campo':<35}| Valor |")
    ln(f"| {'':-<35}| ----- |")
    _e_pct = datos.get("equity_weight_pct")  # E/(E+D) ya en %
    _d_pct = (100.0 - float(_e_pct)) if _e_pct is not None else None
    _tax_pct = datos.get("tasa_impositiva_pct")   # ya en %
    _kd_at_pct = datos.get("kd_after_tax_pct")    # ya en %

    ln(_row("WACC", _pct_dec(metricas.get("wacc"), decimals=2)))
    ln(_row("Ke (costo equity, CAPM)", _pct(metricas.get("capm_pct") or metricas.get("capm") and metricas["capm"]*100, decimals=2)))
    ln(_row("Kd (costo deuda)", _pct_dec(metricas.get("kd") or datos.get("cost_of_debt"), decimals=2)))
    ln(_row("Kd after-tax", _pct(_kd_at_pct, decimals=2) if _kd_at_pct is not None else "(no expuesto en context)"))
    ln(_row("Rf (tasa libre de riesgo)", _pct(metricas.get("tasa_rf_pct") or (metricas.get("tasa_rf") and metricas["tasa_rf"]*100), decimals=2)))
    ln(_row("Rm (retorno esperado mercado)", _pct(metricas.get("market_return_pct") or (metricas.get("market_return") and metricas["market_return"]*100), decimals=2)))
    ln(_row("Beta", _fmt(metricas.get("beta") or datos.get("beta"))))
    ln(_row("Tax rate efectivo", _pct(_tax_pct, decimals=1) if _tax_pct is not None else "(no expuesto en context)"))
    ln(_row("Fuente Tax Rate", _s(datos.get("tasa_impositiva_fuente"))))
    ln(_row("Equity (market cap)", _money(metricas.get("equity") or datos.get("market_cap"))))
    ln(_row("Deuda total", _money(metricas.get("debt") or datos.get("deuda_total"))))
    ln(_row("Peso equity (E/V)", _pct(_e_pct, decimals=1) if _e_pct is not None else "(no expuesto en context)"))
    ln(_row("Peso deuda (D/V)", _pct(_d_pct, decimals=1) if _d_pct is not None else "(no expuesto en context)"))
    ln(_row("Fuente Kd", _s(datos.get("cost_of_debt_fuente"))))
    ln(_row("WACC bajo Rf (aviso)", _s(metricas.get("wacc_below_rf_aviso"), fallback="")))
    ln()

    # ── 4. PROYECCIÓN FCF ─────────────────────────────────────────────────────
    ln(_section("4. Proyección de FCF (DCF Simple)"))
    ln(f"| {'Campo':<35}| Valor |")
    ln(f"| {'':-<35}| ----- |")
    _cagr_cap = metricas.get("cagr_cap_applied")
    _crec_pct = metricas.get("crecimiento_pct")          # ya en %
    _cagr_pct = metricas.get("crecimiento_cagr_pct")     # ya en %
    _g_term_pct = datos.get("g_terminal_pct")             # ya en %

    ln(_row("FCF base TTM", _money(datos.get("fcf_ttm") or datos.get("fcf_actual"))))
    ln(_row("Método de crecimiento", _s(datos.get("metodo_crecimiento") or metricas.get("metodo_crecimiento"))))
    ln(_row("Código método", _s(datos.get("metodo_crecimiento_codigo"))))
    ln(_row("Tasa crecimiento base", _pct(_crec_pct, decimals=2)))
    ln(_row("g terminal (perpetuidad)", _pct(_g_term_pct, decimals=2)))
    ln(_row("Cap CAGR aplicado", "Sí" if _cagr_cap else "No"))
    ln(_row("CAGR antes del cap", _pct(_cagr_pct, decimals=2) if _cagr_cap else f"{_pct(_cagr_pct, decimals=2)} (sin cap)"))
    ln()

    # Detect if FCF series values are stored in billions (common in this codebase).
    # Heuristic: if the first historical value is < 100k but fcf_ttm (absolute) is > 1M,
    # values are in billions. Scale accordingly.
    fcf_ttm_abs = datos.get("fcf_ttm")
    _fcf_hist_raw = resultado.get("fcf_historico") or []
    _first_hist_val = None
    if _fcf_hist_raw:
        e0 = _fcf_hist_raw[0]
        if isinstance(e0, dict):
            _first_hist_val = e0.get("valor") or e0.get("value")
        elif isinstance(e0, (int, float)):
            _first_hist_val = e0
        else:
            _first_hist_val = getattr(e0, "valor", None)
    _fcf_scale = 1.0
    if (
        fcf_ttm_abs is not None and _first_hist_val is not None
        and abs(float(fcf_ttm_abs)) > 1e6 and abs(float(_first_hist_val)) < 1e4
    ):
        _fcf_scale = 1e9

    fcf_proyectado = resultado.get("fcf_proyectado") or []
    if fcf_proyectado:
        ln(_section("4.1 FCF Proyectado año a año", level=3))
        ln(_table_header(["Año", "FCF Proyectado", "FCF descontado"]))
        for i, entry in enumerate(fcf_proyectado, start=1):
            if isinstance(entry, dict):
                yr_label = str(entry.get("anio") or f"Año {i}")
                fcf_v = entry.get("valor") or entry.get("fcf") or entry.get("value")
                desc_v = entry.get("descontado") or entry.get("valor_descontado")
            else:
                yr_label = f"Año {i}"
                try:
                    fcf_v = float(entry)
                except (TypeError, ValueError):
                    fcf_v = None
                desc_v = None
            scaled_v = (float(fcf_v) * _fcf_scale) if fcf_v is not None else None
            scaled_d = (float(desc_v) * _fcf_scale) if desc_v is not None else None
            desc_display = _money(scaled_d) if scaled_d is not None else "(no expuesto en context)"
            ln(f"| {yr_label} | {_money(scaled_v)} | {desc_display} |")
        ln()

    ln(f"| {'Campo':<35}| Valor |")
    ln(f"| {'':-<35}| ----- |")
    _vt = metricas.get("valor_terminal")
    _vtd = metricas.get("valor_terminal_descontado")
    ln(_row("Valor terminal", _money(float(_vt) * _fcf_scale) if _vt is not None else "(no expuesto en context)"))
    ln(_row("Valor terminal descontado", _money(float(_vtd) * _fcf_scale) if _vtd is not None else "(no expuesto en context)"))
    ln(_row("Valor intrínseco por acción", _price(resultado.get("valor_intrinseco"))))
    ln(_row("Acciones (usadas)", _fmt(datos.get("acciones"), decimals=0, suffix="")))
    ln(_row("Deuda neta total", _money(datos.get("deuda_neta_total") or datos.get("deuda_neta"))))
    aviso_acc = datos.get("acciones_ajuste_aviso")
    if aviso_acc:
        ln()
        ln(f"> **Aviso acciones:** {aviso_acc}")
    ln()

    # ── 5. FCF HISTÓRICO ──────────────────────────────────────────────────────
    ln(_section("5. FCF Histórico"))
    fcf_hist = _fcf_hist_raw
    if fcf_hist:
        ln(_table_header(["Año", "FCF"]))
        for i, entry in enumerate(fcf_hist):
            if isinstance(entry, dict):
                yr = (entry.get("anio") or entry.get("año") or entry.get("year")
                      or entry.get("period") or "?")
                val = entry.get("valor") or entry.get("value") or entry.get("fcf")
            elif isinstance(entry, (int, float)):
                yr = f"T-{i}"
                val = entry
            else:
                yr = getattr(entry, "anio", None) or getattr(entry, "year", f"T-{i}")
                val = getattr(entry, "valor", None) or getattr(entry, "value", None)
            scaled = (float(val) * _fcf_scale) if val is not None else None
            ln(f"| {yr} | {_money(scaled)} |")
    else:
        ln("_Sin datos de FCF histórico._")
    ln()

    # Ratios de crecimiento FCF
    crec = datos.get("crecimientos_fcf") or metricas.get("crecimientos_fcf") or {}
    if crec:
        ln(_section("5.1 Tasas de Crecimiento FCF", level=3))
        ln(_table_header(["Período", "Tasa"]))
        for k in sorted(crec.keys()):
            ln(f"| {k} | {_pct(crec[k], decimals=2)} |")
        ln()

    # ── 6. DATOS FINANCIEROS CRUDOS ───────────────────────────────────────────
    ln(_section("6. Datos Financieros"))
    ln(f"| {'Campo':<35}| Valor |")
    ln(f"| {'':-<35}| ----- |")
    campos_financieros = [
        ("Revenue TTM", "revenue_ttm"),
        ("Revenue TTM (display)", "revenue_ttm_display"),
        ("EBITDA TTM", "ebitda_ttm"),
        ("Net Income TTM", "net_income_ttm"),
        ("FCF TTM", "fcf_ttm"),
        ("FCF/Acción", "fcf_per_share"),
        ("FCF Yield", "fcf_yield_pct"),
        ("Deuda LP", "deuda"),
        ("Deuda corriente", "deuda_corriente"),
        ("Deuda total", "deuda_total"),
        ("Caja", "cash"),
        ("Deuda neta (LP)", "deuda_neta"),
        ("Deuda neta total", "deuda_neta_total"),
        ("Activos corrientes", "total_current_assets"),
        ("Pasivos totales", "total_liabilities"),
        ("Patrimonio neto", "total_equity"),
        ("Activos totales", "total_assets"),
        ("Market Cap", "market_cap"),
        ("EV", "enterprise_value"),
        ("P/E TTM", "pe_ttm"),
        ("P/E Fwd", "pe_fwd"),
        ("P/S TTM", "ps_ttm"),
        ("P/GP TTM", "pgp_ttm"),
        ("P/FCF TTM", "pfcf_ttm"),
        ("EV/EBITDA", "ev_ebitda"),
        ("Dividendo por acción", "dividend_per_share"),
        ("Payout ratio", "payout_ratio_pct"),
        ("ROE", "roe"),
        ("ROA", "roa"),
        ("Margen bruto", "gross_margin"),
        ("Margen operativo", "operating_margin"),
        ("Margen neto", "net_margin"),
        ("Cost of Debt", "cost_of_debt"),
        ("Cost of Debt (display)", "cost_of_debt_pct"),
        ("Fuente Kd", "cost_of_debt_fuente"),
        ("Tax rate", "tax_rate"),
    ]
    for label, key in campos_financieros:
        v = datos.get(key)
        if v is not None and v != "":
            ln(_row(label, str(v)))
    ln()

    # Revenue histórico
    rev_hist = datos.get("revenue_historico") or []
    if rev_hist:
        ln(_section("6.1 Revenue Histórico", level=3))
        ln(_table_header(["Período", "Revenue"]))
        for i, entry in enumerate(rev_hist):
            if isinstance(entry, dict):
                yr = (entry.get("anio") or entry.get("año") or entry.get("year")
                      or entry.get("period") or entry.get("fecha") or f"T-{i}")
                val = (entry.get("valor") or entry.get("value") or entry.get("revenue")
                       or entry.get("ingresos"))
            elif isinstance(entry, (int, float)):
                yr = f"T-{i}"
                val = entry
            else:
                yr = (getattr(entry, "anio", None) or getattr(entry, "año", None)
                      or getattr(entry, "year", f"T-{i}"))
                val = (getattr(entry, "valor", None) or getattr(entry, "value", None)
                       or getattr(entry, "revenue", None))
            ln(f"| {yr} | {_money(val)} |")
        ln()

    # ── 7. LOS 14 MODELOS DE VALUACIÓN ───────────────────────────────────────
    ln(_section("7. Modelos de Valuación (14 modelos)"))

    modelos_dict = (multi_model or {}).get("modelos") or {}
    # orden fijo y consistente
    MODEL_ORDER = [
        "dcf", "pe_trailing", "ps", "pgp", "pfcf_trailing",
        "ev_ebitda", "ddm", "pe_fwd", "pfcf_fwd", "egm",
        "liquidacion", "reverse_dcf", "altman_z", "tam",
    ]
    ln(_table_header(["Código", "Nombre", "Valor", "Relevancia", "Peso etapa", "Peso final %", "Aplicable"]))
    for key in MODEL_ORDER:
        m = modelos_dict.get(key) or {}
        if not m:
            ln(f"| {key} | — | — | — | — | — | No |")
            continue
        ln(
            f"| {key} "
            f"| {_s(m.get('nombre'))} "
            f"| {_price(m.get('valor'))} "
            f"| {_s(m.get('relevancia'))} "
            f"| {_fmt(m.get('peso_raw'), decimals=4)} "
            f"| {_fmt(m.get('peso_pct'), decimals=1)} "
            f"| {'Sí' if m.get('aplicable') else 'No'} |"
        )
    ln()

    # Detalle por modelo
    ln(_section("7.1 Detalle por modelo", level=3))
    for key in MODEL_ORDER:
        m = modelos_dict.get(key) or {}
        if not m:
            continue
        ln(_section(f"`{key}` — {_s(m.get('nombre'))}", level=4))
        ln(f"| {'Campo':<30}| Valor |")
        ln(f"| {'':-<30}| ----- |")
        ln(_row("Valor estimado", _price(m.get("valor")), width=30))
        ln(_row("Upside vs precio actual", _pct(m.get("upside_pct"), signed=True), width=30))
        ln(_row("Relevancia", _s(m.get("relevancia")), width=30))
        ln(_row("Aplicable", str(m.get("aplicable", False)), width=30))
        ln(_row("Peso etapa (raw)", _fmt(m.get("peso_raw"), decimals=4), width=30))
        ln(_row("Peso normalizado (%)", _fmt(m.get("peso_pct"), decimals=2), width=30))
        detalle = m.get("detalle") or m.get("descripcion") or ""
        if detalle:
            ln()
            ln(f"**Detalle de cálculo:**")
            ln()
            ln(f"> {str(detalle).replace(chr(10), '  \\n> ')}")
        nota = m.get("nota") or m.get("aviso") or m.get("warning")
        if nota:
            ln()
            ln(f"> ⚠️ {nota}")
        ln()

    # Modelos filtrados/excluidos
    filtrados_outlier = (multi_model or {}).get("modelos_filtrados_outlier") or []
    if filtrados_outlier:
        ln(_section("7.2 Modelos filtrados como outlier", level=3))
        ln(", ".join(str(k) for k in filtrados_outlier))
        ln()

    invalidados = (multi_model or {}).get("modelos_invalidados_financiera") or []
    if invalidados:
        ln(_section("7.3 Modelos invalidados (estructura financiera)", level=3))
        ln(", ".join(str(k) for k in invalidados))
        ln()

    # ── 8. ALTMAN Z-SCORE ────────────────────────────────────────────────────
    altman = (multi_model or {}).get("altman") or modelos_dict.get("altman_z") or {}
    if altman:
        ln(_section("8. Altman Z-Score"))
        ln(f"| {'Campo':<35}| Valor |")
        ln(f"| {'':-<35}| ----- |")
        ln(_row("Z-Score", _fmt(altman.get("z_score") or altman.get("valor"), decimals=2)))
        ln(_row("Zona", _s(altman.get("zona"))))
        ln(_row("Zona code", _s(altman.get("zona_code"))))
        ln(_row("Disponible", str(altman.get("disponible", False))))
        detalle_az = altman.get("detalle") or ""
        if detalle_az:
            ln()
            ln(f"> {detalle_az}")
        ln()

    # ── 9. REVERSE DCF ───────────────────────────────────────────────────────
    rdcf = modelos_dict.get("reverse_dcf") or (multi_model or {}).get("reverse_dcf") or {}
    if rdcf:
        ln(_section("9. Reverse DCF"))
        ln(f"| {'Campo':<35}| Valor |")
        ln(f"| {'':-<35}| ----- |")
        ln(_row("Tasa de crecimiento implícita", _pct(rdcf.get("g_implicita_pct") or rdcf.get("g_implicita"), decimals=2)))
        ln(_row("Disponible", str(rdcf.get("disponible", False))))
        ln(_row("Relevancia", _s(rdcf.get("relevancia"))))
        detalle_rdcf = rdcf.get("detalle") or ""
        if detalle_rdcf:
            ln()
            ln(f"> {detalle_rdcf}")
        ln()

    # ── 10. TAM ──────────────────────────────────────────────────────────────
    tam = modelos_dict.get("tam") or {}
    if tam:
        ln(_section("10. TAM Asistido"))
        ln(f"| {'Campo':<35}| Valor |")
        ln(f"| {'':-<35}| ----- |")
        ln(_row("Valor estimado", _price(tam.get("valor"))))
        ln(_row("Aplicable", str(tam.get("aplicable", False))))
        ln(_row("Relevancia", _s(tam.get("relevancia"))))
        detalle_tam = tam.get("detalle") or ""
        if detalle_tam:
            ln()
            ln(f"> {detalle_tam}")
        ln()

    # ── 11. ESCENARIOS ───────────────────────────────────────────────────────
    escenarios = resultado.get("escenarios") or {}
    if escenarios:
        ln(_section("11. Escenarios (Base / Optimista / Pesimista)"))
        ln(_table_header(["Escenario", "Crecimiento", "Valor intrínseco", "Upside"]))
        for nombre_esc in ("base", "bull", "bear"):
            esc = escenarios.get(nombre_esc) or {}
            if esc:
                ln(
                    f"| {nombre_esc.capitalize()} "
                    f"| {_pct(esc.get('crecimiento'), decimals=2)} "
                    f"| {_price(esc.get('valor_intrinseco') or esc.get('valor'))} "
                    f"| {_pct(esc.get('upside_pct') or esc.get('diferencia_pct'), signed=True)} |"
                )
        ln()

    # ── 12. TABLA DE SENSIBILIDAD ────────────────────────────────────────────
    tabla = resultado.get("tabla_sensibilidad") or {}
    if tabla:
        ln(_section("12. Tabla de Sensibilidad (WACC × Crecimiento)"))
        crecimientos = tabla.get("crecimientos") or []
        filas = tabla.get("rows") or []   # key is "rows" not "filas"
        precio_actual_ts = tabla.get("precio_actual")
        if precio_actual_ts:
            ln(f"_Precio actual de referencia: {_price(precio_actual_ts)}_")
            ln()
        if crecimientos and filas:
            # crecimientos y waccs ya están en %, no en fracción decimal
            header_cols = ["WACC \\ Crec."] + [f"{c:.2f}%" for c in crecimientos]
            ln(_table_header(header_cols))
            for fila in filas:
                wacc_label = f"{fila.get('wacc'):.2f}%"
                cells = [_price(c) for c in (fila.get("cells") or [])]
                ln("| " + wacc_label + " | " + " | ".join(cells) + " |")
        elif not crecimientos or not filas:
            ln("_(tabla no disponible en context)_")
        ln()

    # ── 13. ANALISTAS ────────────────────────────────────────────────────────
    if analyst_data and analyst_data.get("disponible"):
        ln(_section("13. Estimaciones de Analistas"))
        po = analyst_data.get("precio_objetivo") or {}
        po_dict = po if isinstance(po, dict) else {}
        po_medio = po_dict.get("medio") if po_dict else (po if not isinstance(po, dict) else None)
        po_bajo = po_dict.get("bajo")
        po_alto = po_dict.get("alto")
        po_mediana = po_dict.get("mediana")
        po_upside = po_dict.get("upside_pct") if po_dict else analyst_data.get("upside_pct")
        num_analistas = po_dict.get("num_analistas") or analyst_data.get("num_analistas")
        ln(f"| {'Campo':<35}| Valor |")
        ln(f"| {'':-<35}| ----- |")
        ln(_row("Precio objetivo medio", _price(po_medio)))
        ln(_row("Precio objetivo mediana", _price(po_mediana)))
        ln(_row("Rango analistas", f"{_price(po_bajo)} – {_price(po_alto)}"))
        ln(_row("Núm. analistas", str(num_analistas or "N/D")))
        ln(_row("Upside vs precio actual", _pct(po_upside, signed=True)))
        ln(_row("Rec. consenso", _s(analyst_data.get("recomendacion"))))
        ln()

    # ── 14. INSIDER TRADING ──────────────────────────────────────────────────
    if insider_data and insider_data.get("disponible"):
        ln(_section("14. Insider Trading (últimas transacciones)"))
        transacciones = insider_data.get("transacciones") or []
        if transacciones:
            ln(_table_header(["Fecha", "Nombre", "Cargo", "Tipo", "Acciones", "Precio", "Valor"]))
            for t in transacciones[:20]:  # máximo 20 para no inflar el archivo
                ln(
                    f"| {_s(t.get('fecha'))} "
                    f"| {_s(t.get('nombre'))} "
                    f"| {_s(t.get('cargo'), fallback='')} "
                    f"| {_s(t.get('tipo'))} "
                    f"| {_fmt(t.get('acciones'), decimals=0)} "
                    f"| {_price(t.get('precio'))} "
                    f"| {_money(t.get('valor'))} |"
                )
        else:
            ln("_Sin transacciones recientes reportadas._")
        ln()

    # ── 15. ADR / INFO ADICIONAL ─────────────────────────────────────────────
    adr_info = (multi_model or {}).get("adr_info") or {}
    financiera_info = (multi_model or {}).get("financiera_info") or {}
    avisos = []
    if isinstance(adr_info, dict) and adr_info.get("warning"):
        avisos.append(f"**ADR aviso:** {adr_info['warning']}")
    elif isinstance(adr_info, dict) and adr_info.get("es_adr"):
        avisos.append(f"**ADR:** {adr_info.get('country', '')} — es ADR")
    if isinstance(financiera_info, dict) and financiera_info.get("warning"):
        avisos.append(f"**Brazo financiero aviso:** {financiera_info['warning']}")
    elif isinstance(financiera_info, dict) and financiera_info.get("es_financiera"):
        avisos.append(f"**Brazo financiero detectado** — sector {financiera_info.get('sector', '')}")
    nota_estructura = datos.get("nota_estructura_capital")
    if nota_estructura:
        avisos.append(f"**Estructura capital:** {nota_estructura}")
    moneda_aviso = datos.get("moneda_aviso")
    if moneda_aviso:
        avisos.append(f"**Moneda:** {moneda_aviso}")
    beta_aviso = datos.get("beta_aviso")
    if beta_aviso:
        avisos.append(f"**Beta aviso:** {beta_aviso}")
    wacc_aviso = metricas.get("wacc_below_rf_aviso") or metricas.get("wacc_spread_bajo_aviso")
    if wacc_aviso:
        avisos.append(f"**WACC aviso:** {wacc_aviso}")
    cagr_aviso = metricas.get("cagr_cap_applied")
    if cagr_aviso:
        avisos.append(f"**CAGR cap aplicado:** tasa recortada de {_pct(metricas.get('cagr_antes_cap'))} a {_pct(metricas.get('tasa_crecimiento'))}")

    if avisos:
        ln(_section("15. Avisos y Advertencias"))
        for av in avisos:
            ln(f"- {av}")
        ln()

    # ── 16. MENSAJES DE FUENTE ────────────────────────────────────────────────
    mensajes = resultado.get("mensajes_fuente") or []
    if mensajes:
        ln(_section("16. Mensajes de Fuente de Datos"))
        for msg in mensajes:
            ln(f"- {msg}")
        ln()

    # ── FOOTER ────────────────────────────────────────────────────────────────
    ln("---")
    ln(f"_Generado por Intrinsic — {now.strftime('%Y-%m-%d %H:%M:%S')} — Solo uso admin/QA_")

    return "\n".join(lines)
