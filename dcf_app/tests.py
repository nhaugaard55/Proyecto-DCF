from copy import deepcopy
from unittest.mock import MagicMock, patch

import pandas as pd

from django.test import SimpleTestCase

from dcf_core.DCF_Main import ejecutar_dcf
from dcf_core.finanzas import seleccionar_metodo_crecimiento
from dcf_core.fmp import FMPClientError
from dcf_core.multi_model_valuation import _modelo_reverse_dcf, run_all_models


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


class MultiModelValuationTests(SimpleTestCase):
    def test_tam_model_is_useful_in_startup_stage(self) -> None:
        resultado = run_all_models("TEST", _sample_financials(), stage=1, wacc=0.10)

        tam = resultado["modelos"]["tam"]

        self.assertEqual(tam["relevancia"], "Útil")
        self.assertGreater(tam["peso_raw"], 0)
        self.assertIsNotNone(tam["valor"])
        self.assertIn("tam", resultado["consenso"]["modelos_usados_keys"])

    def test_tam_model_is_excluded_in_capital_return_stage(self) -> None:
        resultado = run_all_models("TEST", _sample_financials(), stage=5, wacc=0.10)

        tam = resultado["modelos"]["tam"]

        self.assertEqual(tam["relevancia"], "No útil")
        self.assertEqual(tam["peso_raw"], 0.0)
        self.assertEqual(tam["peso"], 0.0)
        self.assertNotIn("tam", resultado["consenso"]["modelos_usados_keys"])

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
