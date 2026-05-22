from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from django.test import SimpleTestCase

from dcf_core import insider_trading
from dcf_core.DCF_Main import ejecutar_dcf
from dcf_core.company_stage import detect_company_stage
from dcf_core.finanzas import seleccionar_metodo_crecimiento
from dcf_core.fmp import FMPClientError
from dcf_core.multi_model_valuation import calcular_score_final, _modelo_reverse_dcf, run_all_models


def _sample_financials() -> dict:
    return {
        "valor_intrinseco": 42.0,
        "precio_actual": 20.0,
        "metricas": {
            "crecimiento_pct": 15.0,
            "wacc_pct": 10.0,
            "crecimiento_cagr": 0.15,
        },
        "net_margin": 0.12,
        "datos_empresa": {
            "sector": "Technology",
            "revenue_ttm": 2_000_000_000.0,
            "gross_profit_ttm": 1_100_000_000.0,
            "acciones": 100_000_000.0,
            "eps_ttm": 1.2,
            "eps_forward": 1.5,
            "fcf_ttm": 220_000_000.0,
            "deuda": 100_000_000.0,
        },
    }


class InsiderTradingTests(SimpleTestCase):
    def setUp(self) -> None:
        insider_trading._CACHE.clear()

    def test_finnhub_payload_is_normalized_and_scored_as_bullish(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        payload = [
            {
                "transactionDate": today,
                "name": "Jane CEO",
                "title": "Chief Executive Officer",
                "transactionCode": "P",
                "share": 10_000,
                "transactionPrice": 20,
                "shareOwnedFollowingTransaction": 100_000,
            },
            {
                "transactionDate": today,
                "name": "John Director",
                "title": "Director",
                "transactionCode": "S",
                "share": 1_000,
                "transactionPrice": 10,
                "shareOwnedFollowingTransaction": 40_000,
            },
        ]

        with patch.object(insider_trading, "_fetch_finnhub", return_value=payload), patch.object(
            insider_trading, "_fetch_fmp", return_value=[]
        ):
            result = insider_trading.get_insider_trading("TEST")

        self.assertTrue(result["disponible"])
        self.assertEqual(result["fuente"], "finnhub")
        self.assertEqual(result["score_sentimiento"], "alcista")
        self.assertEqual(result["score_color"], "verde")
        self.assertEqual(result["resumen"]["total_compras"], 1)
        self.assertEqual(result["transacciones"][0]["insider_cargo"], "CEO")
        self.assertEqual(result["transacciones"][0]["tipo"], "compra")

    def test_finnhub_uses_change_as_transaction_shares_when_available(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        payload = [
            {
                "transactionDate": today,
                "name": "Large Holder",
                "isTenPercentOwner": True,
                "transactionCode": "P",
                "change": 100,
                "share": 44_179_216,
                "transactionPrice": 5,
            }
        ]

        with patch.object(insider_trading, "_fetch_finnhub", return_value=payload), patch.object(
            insider_trading, "_fetch_fmp", return_value=[]
        ):
            result = insider_trading.get_insider_trading("TEST")

        tx = result["transacciones"][0]
        self.assertEqual(tx["shares"], 100)
        self.assertEqual(tx["shares_restantes"], 44_179_216)
        self.assertEqual(tx["valor_total"], 500)
        self.assertEqual(result["resumen"]["valor_compras_usd"], 500)

    def test_form4_non_market_codes_are_labeled_without_affecting_score(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        payload = [
            {
                "transactionDate": today,
                "name": "Awarded Officer",
                "title": "Chief Legal Officer",
                "transactionCode": "A",
                "change": 1_000,
                "transactionPrice": 0,
            },
            {
                "transactionDate": today,
                "name": "Tax Officer",
                "title": "Chief Legal Officer",
                "transactionCode": "F",
                "change": -100,
                "transactionPrice": 0,
            },
        ]

        with patch.object(insider_trading, "_fetch_finnhub", return_value=payload), patch.object(
            insider_trading, "_fetch_fmp", return_value=[]
        ):
            result = insider_trading.get_insider_trading("TEST")

        tipos = {tx["tipo"]: tx for tx in result["transacciones"]}
        self.assertEqual(tipos["adjudicacion"]["tipo_label"], "Adjudicación")
        self.assertEqual(tipos["retencion_impuestos"]["tipo_label"], "Retención imp.")
        self.assertEqual(tipos["adjudicacion"]["precio"], 0)
        self.assertEqual(tipos["adjudicacion"]["precio_display"], "$0.00")
        self.assertEqual(result["score_sentimiento"], "neutral")
        self.assertEqual(result["resumen"]["valor_compras_usd"], 0)

    def test_falls_back_to_fmp_when_finnhub_is_empty(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        fmp_payload = [
            {
                "transactionDate": today,
                "reportingName": "Jane CFO",
                "typeOfOwner": "Chief Financial Officer",
                "transactionType": "S",
                "securitiesTransacted": 2_000,
                "price": 15,
            }
        ]

        with patch.object(insider_trading, "_fetch_finnhub", return_value=[]), patch.object(
            insider_trading, "_fetch_fmp", return_value=fmp_payload
        ):
            result = insider_trading.get_insider_trading("TEST")

        self.assertTrue(result["disponible"])
        self.assertEqual(result["fuente"], "fmp")
        self.assertEqual(result["score_sentimiento"], "bajista")
        self.assertEqual(result["transacciones"][0]["insider_cargo"], "CFO")

    def test_missing_finnhub_role_is_enriched_from_fmp_by_name(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        finnhub_payload = [
            {
                "transactionDate": today,
                "name": "Jane Officer",
                "transactionCode": "S",
                "share": 100,
                "transactionPrice": 20,
            }
        ]
        fmp_payload = [
            {
                "transactionDate": today,
                "reportingName": "Jane Officer",
                "typeOfOwner": "Vice President",
                "transactionType": "S",
                "securitiesTransacted": 100,
                "price": 20,
            }
        ]

        with patch.object(insider_trading, "_fetch_finnhub", return_value=finnhub_payload), patch.object(
            insider_trading, "_fetch_fmp", return_value=fmp_payload
        ), patch.object(insider_trading, "_enriquecer_cargos_desde_sec"):
            result = insider_trading.get_insider_trading("TEST")

        self.assertEqual(result["fuente"], "finnhub")
        self.assertEqual(result["transacciones"][0]["insider_cargo"], "VP")

    def test_sec_form4_officer_title_is_normalized(self) -> None:
        html = """
        <td align="center"><span class="FormData">X</span></td>
        <td class="MedSmallFormText">Officer (give title below)</td>
        <td width="35%" align="left" style="color: blue">SVP - Chief Accounting Officer</td>
        """

        self.assertEqual(insider_trading._extraer_cargo_sec_html(html), "VP")

    def test_sec_form4_director_is_labeled_as_board_member(self) -> None:
        html = """
        <td align="center"><span class="FormData">X</span></td>
        <td class="MedSmallFormText">Director</td>
        """

        self.assertEqual(
            insider_trading._extraer_cargo_sec_html_detalle(html),
            ("Director", "Miembro del directorio"),
        )

    def test_sec_form4_ten_percent_owner_is_labeled(self) -> None:
        html = """
        <td align="center"><span class="FormData">X</span></td>
        <td class="MedSmallFormText">10% Owner</td>
        """

        self.assertEqual(
            insider_trading._extraer_cargo_sec_html_detalle(html),
            ("Accionista >10%", "Accionista >10%"),
        )

    def test_role_is_propagated_to_same_insider_transactions(self) -> None:
        transacciones = [
            {
                "insider_nombre": "MAESTRINI ANDRE",
                "insider_cargo": "Presidente",
                "insider_cargo_detalle": "Pres, CCO & Interim Co-CEO",
                "insider_cargo_fuente": "finnhub",
            },
            {
                "insider_nombre": "MAESTRINI ANDRE",
                "insider_cargo": "N/D",
            },
            {
                "insider_nombre": "NEUBURGER NICOLE",
                "insider_cargo": "N/D",
            },
            {
                "insider_nombre": "NEUBURGER NICOLE",
                "insider_cargo": "Chief Brand Officer",
                "insider_cargo_detalle": "Chief Brand Officer",
                "insider_cargo_fuente": "sec",
            },
        ]

        insider_trading._propagar_cargos_por_insider(transacciones)

        self.assertEqual(transacciones[1]["insider_cargo"], "Presidente")
        self.assertEqual(transacciones[1]["insider_cargo_detalle"], "Pres, CCO & Interim Co-CEO")
        self.assertEqual(transacciones[2]["insider_cargo"], "Chief Brand Officer")

    def test_returns_unavailable_when_transactions_are_old(self) -> None:
        old_date = (datetime.now(timezone.utc).date() - timedelta(days=181)).isoformat()
        payload = [{"transactionDate": old_date, "name": "Old Insider", "transactionCode": "P", "share": 1}]

        with patch.object(insider_trading, "_fetch_finnhub", return_value=payload), patch.object(
            insider_trading, "_fetch_fmp", return_value=[]
        ):
            result = insider_trading.get_insider_trading("TEST")

        self.assertFalse(result["disponible"])
        self.assertIn("últimos 180 días", result["mensaje"])

    def test_sale_near_option_exercise_gets_reduced_weight(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        payload = [
            {
                "transactionDate": today,
                "name": "Executive One",
                "title": "CEO",
                "transactionCode": "M",
                "share": 10_000,
                "transactionPrice": 125,
            },
            {
                "transactionDate": today,
                "name": "Executive One",
                "title": "CEO",
                "transactionCode": "S",
                "share": 10_000,
                "transactionPrice": 213,
            },
            {
                "transactionDate": today,
                "name": "Executive One",
                "title": "CEO",
                "transactionCode": "P",
                "share": 1_000,
                "transactionPrice": 200,
            },
        ]

        with patch.object(insider_trading, "_fetch_finnhub", return_value=payload), patch.object(
            insider_trading, "_fetch_fmp", return_value=[]
        ):
            result = insider_trading.get_insider_trading("TEST")

        sale = next(tx for tx in result["transacciones"] if tx["tipo"] == "venta")
        self.assertTrue(sale["venta_relacionada_ejercicio"])
        self.assertEqual(sale["tipo_extendido"], "venta post-ejercicio")
        self.assertEqual(result["resumen"]["valor_ventas_usd"], 2_130_000)
        self.assertEqual(result["resumen"]["valor_ventas_ajustado_usd"], 532_500)
        self.assertEqual(result["resumen"]["ventas_post_ejercicio_usd"], 2_130_000)
        self.assertEqual(result["resumen"]["porcentaje_ventas_ajustadas_sobre_brutas"], 25)
        self.assertTrue(result["resumen"]["advertencia_ventas_compensacion"])

    def test_automatic_sale_gets_lowest_sale_weight(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        payload = [
            {
                "transactionDate": today,
                "name": "Executive Two",
                "title": "VP",
                "transactionCode": "S",
                "share": 1_000,
                "transactionPrice": 100,
                "footnote": "Sale made pursuant to Rule 10b5-1 planned sale.",
            }
        ]

        with patch.object(insider_trading, "_fetch_finnhub", return_value=payload), patch.object(
            insider_trading, "_fetch_fmp", return_value=[]
        ):
            result = insider_trading.get_insider_trading("TEST")

        self.assertTrue(result["transacciones"][0]["plan_automatico"])
        self.assertEqual(result["resumen"]["valor_ventas_usd"], 100_000)
        self.assertEqual(result["resumen"]["valor_ventas_ajustado_usd"], 15_000)
        self.assertEqual(result["resumen"]["ventas_automaticas_usd"], 100_000)
        self.assertEqual(result["resumen"]["porcentaje_ventas_ajustadas_sobre_brutas"], 15)


class MultiModelValuationTests(SimpleTestCase):
    def test_tam_model_is_scenario_only_in_startup_stage(self) -> None:
        resultado = run_all_models("TEST", _sample_financials(), stage=1, wacc=0.10)

        tam = resultado["modelos"]["tam"]

        self.assertEqual(tam["peso_raw"], 0.0)
        self.assertEqual(tam["peso"], 0.0)
        self.assertEqual(tam["modo"], "escenario")
        self.assertFalse(tam["aplicable"])
        self.assertIsNotNone(tam["valor"])
        self.assertNotIn("tam", resultado["consenso"]["modelos_usados_keys"])

    def test_tam_model_is_excluded_in_capital_return_stage(self) -> None:
        resultado = run_all_models("TEST", _sample_financials(), stage=5, wacc=0.10)

        tam = resultado["modelos"]["tam"]

        self.assertEqual(tam["relevancia"], "No útil")
        self.assertEqual(tam["peso_raw"], 0.0)
        self.assertEqual(tam["peso"], 0.0)
        self.assertNotIn("tam", resultado["consenso"]["modelos_usados_keys"])

    def test_decline_stage_prioritizes_asset_and_credit_models(self) -> None:
        financials = _sample_financials()
        financials["datos_empresa"].update({
            "payout_ratio": 0.95,
            "total_current_assets": 700_000_000.0,
            "total_liabilities": 350_000_000.0,
            "ebitda_ttm": 180_000_000.0,
            "deuda_neta": 50_000_000.0,
        })
        financials["net_margin"] = 0.04

        resultado = run_all_models("TEST", financials, stage=6, wacc=0.10)

        self.assertEqual(resultado["modelos"]["dcf"]["relevancia"], "No útil")
        self.assertEqual(resultado["modelos"]["liquidation_value"]["relevancia"], "Útil")
        self.assertEqual(resultado["modelos"]["ev_ebitda"]["relevancia"], "Útil")
        self.assertEqual(resultado["modelos"]["pfcf_trailing"]["relevancia"], "Algo útil")
        self.assertEqual(resultado["modelos"]["ddm"]["relevancia"], "No útil")
        self.assertIn("advertencia", resultado["modelos"]["pfcf_trailing"])

    def test_final_score_combines_consensus_solvency_and_filters(self) -> None:
        score = calcular_score_final(
            {
                "disponible": True,
                "modelos_usados": 3,
                "precio": 118.0,
                "precio_actual": 100.0,
                "disagreement_ratio": 0.142,
            },
            {"disponible": True, "z_score": 5.68},
            [
                {"cumple": True},
                {"cumple": True},
                {"cumple": True},
                {"cumple": True},
                {"cumple": False},
                {"cumple": False},
            ],
            stage=4,
        )

        self.assertEqual(score["score"], 7.6)
        self.assertEqual(score["recomendacion"], "Comprar")
        self.assertEqual(score["componentes"]["upside"]["puntos"], 7.5)
        self.assertEqual(score["componentes"]["confianza"]["puntos"], 7.0)
        self.assertEqual(score["componentes"]["solvencia"]["puntos"], 10.0)
        self.assertEqual(score["componentes"]["fundamentals"]["puntos"], 6.0)

    def test_final_score_uses_neutral_points_for_missing_data(self) -> None:
        score = calcular_score_final({}, {}, None, stage=2)

        self.assertEqual(score["score"], 5.0)
        self.assertEqual(score["recomendacion"], "Mantener")
        self.assertEqual(score["componentes"]["upside"]["puntos"], 5.0)
        self.assertEqual(score["componentes"]["confianza"]["puntos"], 5.0)
        self.assertEqual(score["componentes"]["solvencia"]["puntos"], 5.0)
        self.assertEqual(score["componentes"]["fundamentals"]["puntos"], 5.0)
        self.assertEqual(
            score["nota_etapa"],
            "Score orientativo — en etapas tempranas la incertidumbre es muy alta",
        )

    def test_final_score_caps_decline_with_distress_z_score(self) -> None:
        score = calcular_score_final(
            {
                "disponible": True,
                "modelos_usados": 4,
                "precio": 150.0,
                "precio_actual": 100.0,
                "disagreement_ratio": 0.05,
            },
            {"disponible": True, "z_score": 1.2},
            [{"cumple": True}, {"cumple": True}, {"cumple": True}, {"cumple": True}],
            stage=6,
        )

        self.assertEqual(score["score"], 4.0)
        self.assertEqual(score["recomendacion"], "Mantener")
        self.assertEqual(
            score["nota_etapa"],
            "Empresa en declive con riesgo de insolvencia — score limitado",
        )

    def test_reverse_dcf_allows_explicit_growth_above_wacc(self) -> None:
        financials = {
            "precio_actual": 30.0,
            "metricas": {"crecimiento_cagr": 0.08},
            "datos_empresa": {
                "acciones": 100_000_000.0,
                "fcf_ttm": 100_000_000.0,
                "deuda": 0.0,
                "deuda_neta": 0.0,
            },
        }

        reverse_dcf = _modelo_reverse_dcf(financials, 0.10)

        self.assertTrue(reverse_dcf["aplicable"])
        self.assertGreater(reverse_dcf["g_implicita_pct"], 10.0)
        self.assertIn("crecimiento explícito permitido", reverse_dcf["detalle"])

    def test_reverse_dcf_uses_net_debt_when_available(self) -> None:
        base = {
            "precio_actual": 30.0,
            "metricas": {"crecimiento_cagr": 0.08},
            "datos_empresa": {
                "acciones": 100_000_000.0,
                "fcf_ttm": 100_000_000.0,
                "deuda": 0.0,
                "deuda_neta": 0.0,
            },
        }
        higher_net_debt = deepcopy(base)
        higher_net_debt["datos_empresa"]["deuda_neta"] = 500_000_000.0

        reverse_base = _modelo_reverse_dcf(base, 0.10)
        reverse_higher_net_debt = _modelo_reverse_dcf(higher_net_debt, 0.10)

        self.assertTrue(reverse_base["aplicable"])
        self.assertTrue(reverse_higher_net_debt["aplicable"])
        self.assertGreater(
            reverse_higher_net_debt["g_implicita_pct"],
            reverse_base["g_implicita_pct"],
        )


class CompanyStageDetectionTests(SimpleTestCase):
    def test_high_revenue_growth_with_scale_is_hyper_growth_despite_negative_fcf(self) -> None:
        financials = {
            "revenue_growth_raw": 1.104,
            "net_margin": -0.227,
            "has_dividends": False,
            "fcf_historico": [
                {"anio": 2024, "valor": -1.45},
                {"anio": 2023, "valor": -0.91},
                {"anio": 2022, "valor": -0.37},
            ],
            "metricas": {"crecimiento_cagr": None},
            "datos_empresa": {"revenue_ttm": 5_000_000_000.0},
            "filtros": [],
        }

        stage = detect_company_stage("CRWV", financials)

        self.assertEqual(stage["stage"], 2)
        self.assertEqual(stage["stage_name"], "Hyper Growth")
        self.assertGreater(stage["scores"][2], stage["scores"][1])

    def test_decline_financial_override_degrades_capital_return(self) -> None:
        financials = {
            "revenue_growth_raw": 0.01,
            "net_margin": -0.04,
            "has_dividends": True,
            "fcf_historico": [
                {"anio": 2024, "valor": 1.4},
                {"anio": 2023, "valor": 1.3},
                {"anio": 2022, "valor": 1.2},
            ],
            "metricas": {"crecimiento_cagr": 0.04},
            "datos_empresa": {
                "revenue_ttm": 40_000_000_000.0,
                "fcf_ttm": 1_400_000_000.0,
                "roe_raw": -0.12,
                "debt_to_capital": 0.72,
            },
            "filtros": [],
        }

        stage = detect_company_stage("F", financials)

        self.assertEqual(stage["stage"], 6)
        self.assertEqual(stage["confidence"], "Media")
        self.assertEqual(stage["stage_overrides"][0]["tipo"], "A")

    def test_decline_secular_override_catches_physical_retail(self) -> None:
        financials = {
            "revenue_growth_raw": -0.035,
            "net_margin": 0.015,
            "has_dividends": True,
            "fcf_historico": [
                {"anio": 2024, "valor": 0.55},
                {"anio": 2023, "valor": 0.50},
                {"anio": 2022, "valor": 0.45},
            ],
            "metricas": {"crecimiento_cagr": 0.06},
            "datos_empresa": {
                "sector": "Consumer Cyclical",
                "industria": "Department Stores",
                "revenue_ttm": 23_000_000_000.0,
                "fcf_ttm": 550_000_000.0,
                "pe_ratio_raw": 6.5,
                "pb_ratio_raw": 0.8,
            },
            "filtros": [],
        }

        stage = detect_company_stage("M", financials)

        self.assertEqual(stage["stage"], 6)
        self.assertEqual(stage["confidence"], "Media")
        self.assertEqual(stage["stage_overrides"][0]["tipo"], "B")
        self.assertIn("Decline secular", stage["stage_notes"][0])

    def test_margin_compression_warning_lowers_confidence_without_degrading(self) -> None:
        financials = {
            "revenue_growth_raw": -0.01,
            "net_margin": 0.09,
            "has_dividends": True,
            "fcf_historico": [
                {"anio": 2024, "valor": 2.4},
                {"anio": 2023, "valor": 2.0},
                {"anio": 2022, "valor": 1.6},
            ],
            "metricas": {"crecimiento_cagr": 0.20},
            "datos_empresa": {
                "revenue_ttm": 30_000_000_000.0,
                "fcf_ttm": 2_400_000_000.0,
                "pe_ratio_raw": 18.0,
                "pb_ratio_raw": 2.2,
                "gross_margin_trend": -0.02,
            },
            "filtros": [],
        }

        stage = detect_company_stage("MIXED", financials)

        self.assertNotEqual(stage["stage"], 6)
        self.assertEqual(stage["confidence"], "Baja")
        self.assertEqual(stage["stage_overrides"][0]["tipo"], "C")
        self.assertIn("desinversión", stage["manual_review_warnings"][0])


class AutomaticAnalysisTests(SimpleTestCase):
    def test_selects_growth_method_closest_to_zero(self) -> None:
        codigo, nombre, tasa = seleccionar_metodo_crecimiento(0.18, 0.07)
        self.assertEqual(codigo, "2")
        self.assertEqual(nombre, "Promedio")
        self.assertEqual(tasa, 0.07)

        codigo, nombre, tasa = seleccionar_metodo_crecimiento(-0.03, 0.06)
        self.assertEqual(codigo, "1")
        self.assertEqual(nombre, "CAGR")
        self.assertEqual(tasa, -0.03)

    @patch("dcf_core.DCF_Main.calcular_tabla_sensibilidad", return_value=None)
    @patch("dcf_core.DCF_Main.calcular_escenarios", return_value=None)
    @patch("dcf_core.DCF_Main.calcular_crecimientos", return_value=(0.12, 0.03))
    @patch("dcf_core.DCF_Main.analizar_empresa")
    @patch(
        "dcf_core.DCF_Main._obtener_metricas_yfinance",
        return_value=(0.24, {2024: 0.24}, 0.05, {2024: 0.05}),
    )
    @patch(
        "dcf_core.DCF_Main.obtener_metricas_financieras",
        side_effect=FMPClientError("FMP sin acceso"),
    )
    @patch("dcf_core.DCF_Main._obtener_fcf_yfinance", return_value=[100.0, 90.0, 80.0, 70.0])
    @patch(
        "dcf_core.DCF_Main.obtener_fcf_historico",
        side_effect=FMPClientError("FMP sin acceso"),
    )
    @patch("dcf_core.DCF_Main.yf.Ticker")
    def test_execute_dcf_prioritizes_fmp_and_falls_back_to_yfinance(
        self,
        mock_ticker,
        _mock_fcf_fmp,
        _mock_fcf_yf,
        _mock_metricas_fmp,
        _mock_metricas_yf,
        mock_analizar_empresa,
        _mock_crecimientos,
        _mock_escenarios,
        _mock_sensibilidad,
    ) -> None:
        fake_empresa = MagicMock()
        fake_empresa.history.return_value = pd.DataFrame()
        mock_ticker.return_value = fake_empresa

        mock_analizar_empresa.return_value = {
            "nombre": "Test Corp",
            "sector": "Technology",
            "precio_actual": 10.0,
            "valor_intrinseco": 12.0,
            "diferencia_pct": 20.0,
            "estado": "SUBVALUADA",
            "datos_empresa": {
                "metodo_crecimiento": "Promedio",
                "metodo_crecimiento_codigo": "2",
                "deuda": 0.0,
                "acciones": 100.0,
            },
            "metricas": {"wacc": 0.08, "crecimiento": 0.03},
        }

        resultado = ejecutar_dcf("TEST")

        self.assertEqual(mock_analizar_empresa.call_args.args[1], "auto")
        self.assertEqual(resultado["fuente_datos"], "yfinance")
        self.assertEqual(resultado["fuente_solicitada"], "auto")
        self.assertEqual(_mock_escenarios.call_args.args[1], 0.03)
        self.assertEqual(_mock_sensibilidad.call_args.args[2], 0.03)
        self.assertIn(
            "Se utilizó Yfinance porque Financial Modeling Prep devolvió un error",
            resultado["mensaje_fuente"],
        )
