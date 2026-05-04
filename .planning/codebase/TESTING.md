# Testing

## Test Setup

- **Framework**: Django's built-in `unittest`-based test runner via `manage.py test`.
- **Base class**: `django.test.SimpleTestCase` — used for all existing tests. This class explicitly avoids database access, which is appropriate since all tests target `dcf_core` pure-Python logic.
- **Mocking**: `unittest.mock` (`patch`, `MagicMock`) from the standard library. Used to mock external API calls (FMP, yfinance) so tests run without network access.
- **Test file location**: single file at `dcf_app/tests.py`. There are no test files in `dcf_core/`.
- **No pytest**, no `conftest.py`, no test configuration files (`.pytest.ini`, `setup.cfg [tool:pytest]`, etc.).
- **No CI configuration** visible (no `.github/workflows/`, no `Makefile` with test targets).

---

## Test Types

Only **unit tests** are present. There are no integration tests, end-to-end tests, view tests, or model tests.

The tests fall into three logical groups, each in its own `SimpleTestCase` subclass:

### `MultiModelValuationTests` (4 tests)
Tests for `dcf_core/multi_model_valuation.py`:
- Verifies TAM model relevance and weight in startup stage (stage 1).
- Verifies TAM model is excluded (weight=0, relevance="No útil") in capital return stage (stage 5).
- Verifies Reverse DCF handles explicit growth above WACC correctly.
- Verifies Reverse DCF uses `deuda_neta` (net debt) when available, producing higher implied growth than with zero debt.

### `CompanyStageDetectionTests` (1 test)
Tests for `dcf_core/company_stage.py`:
- Verifies that a company with high revenue growth (>100%) and at-scale revenue is classified as Hyper Growth (stage 2) even when FCF is negative.

### `AutomaticAnalysisTests` (2 tests)
Tests for `dcf_core/finanzas.py` and `dcf_core/DCF_Main.py`:
- Verifies `seleccionar_metodo_crecimiento` picks the rate closest to zero (most conservative).
- Verifies `ejecutar_dcf` prioritizes FMP data and falls back to yfinance when FMP raises `FMPClientError`, including the correct `mensaje_fuente` in the result dict. This test uses 9 `@patch` decorators to mock the full external dependency chain.

---

## Test Patterns

### Fixture helper
A module-level `_sample_financials()` function returns a canonical `dict` representing a healthy Technology-sector company. Used as the base input for `MultiModelValuationTests`.

### deepcopy for mutation tests
`test_reverse_dcf_uses_net_debt_when_available` uses `copy.deepcopy` to create a modified variant of the base dict without mutating the original, then compares model outputs between the two variants.

### Assertion style
Standard `unittest` assertions are used:
- `assertEqual`, `assertGreater`, `assertIsNotNone`, `assertIn`, `assertTrue`
- No `assertRaises` pattern (no tests for error paths).

### Mock chaining with `@patch`
The FMP fallback test stacks 9 `@patch` decorators in reverse order (innermost first as function args). Side effects are set with `side_effect=FMPClientError(...)` to simulate API failures, and `return_value=...` to supply fake data for yfinance paths.

---

## Coverage

**No coverage tooling is configured** (no `.coveragerc`, no `pytest-cov`, no coverage reports).

Based on the test file content, coverage is narrow:

| Module | What is tested |
|---|---|
| `dcf_core/multi_model_valuation.py` | `run_all_models`, `_modelo_reverse_dcf` (partial) |
| `dcf_core/company_stage.py` | `detect_company_stage` (one scenario) |
| `dcf_core/finanzas.py` | `seleccionar_metodo_crecimiento` |
| `dcf_core/DCF_Main.py` | `ejecutar_dcf` (FMP→yfinance fallback path) |
| `dcf_app/views.py` | **Not tested** |
| `dcf_app/models.py` | **Not tested** |
| `dcf_core/empresa.py` | **Not tested** |
| `dcf_core/fmp.py` | **Not tested** |
| `dcf_core/ai_summary.py` | **Not tested** |
| `dcf_core/search.py` | **Not tested** |
| `dcf_core/business_cycle.py` | **Not tested** |
| `dcf_core/marketaux.py` | **Not tested** |
| `dcf_core/finnhub.py` | **Not tested** |

Total test count: **7 test methods**.

---

## How to Run Tests

```bash
# From the project root (where manage.py lives)
python manage.py test dcf_app

# Or run all discovered tests
python manage.py test

# Run a specific test class
python manage.py test dcf_app.tests.MultiModelValuationTests

# Run a specific test method
python manage.py test dcf_app.tests.AutomaticAnalysisTests.test_execute_dcf_prioritizes_fmp_and_falls_back_to_yfinance
```

No virtual environment activation command is standardized in the project. The venv is at `venv/` in the project root — activate with `source venv/bin/activate` before running tests.

`SimpleTestCase` is used, so no database setup (`--keepdb`, migrations, fixtures) is required.
