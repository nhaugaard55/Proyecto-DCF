from django.test import SimpleTestCase

from dcf_core.multi_model_valuation import run_all_models


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
