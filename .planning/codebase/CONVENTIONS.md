# Conventions

## Naming Conventions

### Python (backend)

- **Files and modules**: lowercase with underscores (`dcf_core/multi_model_valuation.py`, `dcf_core/company_stage.py`).
- **Classes**: PascalCase (`FMPClient`, `FMPClientError`, `AnalysisRecord`, `WatchlistItem`, `AISummaryError`).
- **Functions**: lowercase with underscores (`calcular_wacc`, `proyectar_fcf`, `detect_company_stage`, `run_all_models`).
- **Private / internal functions**: single leading underscore (`_cached_ejecutar_dcf`, `_resolver_ticker`, `_to_decimal`, `_modelo_dcf`, `_sf`, `_redistribuir_pesos`).
- **Constants and module-level config**: ALL_CAPS with underscores for fixed values (`G_TERMINAL`, `NEWS_PAGE_SIZE`, `_DCF_CACHE_TTL`, `_PREFETCH_TIMEOUT`).
- **Variables in Spanish**: domain variables use Spanish (`ticker`, `valor_intrinseco`, `precio_actual`, `diferencia_pct`, `crecimiento`, `fuente_datos`). Internal algorithmic helpers and dataclass fields use English (`year`, `value`, `tax_rate`, `cost_of_debt`).
- **URL names**: lowercase with hyphens in URL path strings; snake_case for `name=` parameter (`'home'`, `'dcf_pdf'`, `'watchlist_toggle'`).
- **Template context keys**: Spanish snake_case (`resultado`, `multi_model`, `company_stage`, `chart_data`, `news_data`, `recent_records`).

### Django models

- Model class names in PascalCase English (`AnalysisRecord`, `WatchlistItem`).
- Field names in snake_case English (`ticker`, `company_name`, `company_exchange`, `created_at`).
- String choices defined as class constants (`METODO_CAGR = "1"`, `METODO_PROMEDIO = "2"`).

---

## Code Style

- **Python version**: 3.12+ (uses `from __future__ import annotations`, `X | Y` union types, `type[X]` syntax).
- **Type hints**: used consistently throughout. `Optional[T]` from `typing` is common; newer union syntax (`str | None`) appears in newer code. Return types annotated on public functions.
- **Imports**: stdlib first, then third-party, then local. Relative imports used within packages (`from .finanzas import ...`).
- **Docstrings**: module-level docstrings on complex modules (`multi_model_valuation.py`, `company_stage.py`, `fmp.py`). Function docstrings on public/exported functions; private helpers typically use a one-line comment or no docstring.
- **Line length**: no enforced limit visible; long lines appear in financial logic (~120-140 chars in some places).
- **Comments**: section separators use 80-dash dividers (`# ---------------------------------------------------------------------------`) for logical groupings within large files.
- **No linter config files** (no `.flake8`, `pyproject.toml`, `.ruff.toml`, etc.) are present. No formatter config (no `black`, `isort` config).

---

## File Organization Patterns

```
Proyecto DCF - 1.4/
├── Proyecto_DCF/          # Django project package (settings, urls, wsgi, asgi)
├── dcf_app/               # Django app: views, models, urls, templates, migrations
│   ├── templates/
│   │   └── dcf_app/
│   │       ├── base.html
│   │       ├── index.html
│   │       ├── comparar.html
│   │       ├── watchlist.html
│   │       ├── pdf_report.html
│   │       └── components/   # reusable template fragments
│   └── tests.py
└── dcf_core/              # Pure Python business logic (no Django dependencies)
    ├── DCF_Main.py        # Orchestrator: executes the full DCF pipeline
    ├── empresa.py         # analizar_empresa() — main financial analysis
    ├── finanzas.py        # WACC, FCF projection, scenario, sensitivity functions
    ├── multi_model_valuation.py  # 9-model valuation engine
    ├── company_stage.py   # Life-cycle stage detection
    ├── ai_summary.py      # Groq/HuggingFace sentiment analysis
    ├── fmp.py             # Financial Modeling Prep API client
    ├── marketaux.py       # Marketaux news API client
    ├── finnhub.py         # Finnhub news API client
    ├── search.py          # Company search (FMP → Yahoo → local index)
    ├── business_cycle.py  # Macro business cycle detection
    ├── exportar.py        # Export helpers
    └── utils.py           # Shared datetime parsing utilities
```

Key pattern: `dcf_core` is intentionally framework-agnostic — it does not import Django. All Django-specific code (views, models, cache, ORM) lives in `dcf_app`. This makes `dcf_core` independently testable with `django.test.SimpleTestCase` (no database).

---

## Common Patterns & Idioms

### Safe float conversion
Every module defines its own local helper for safe numeric conversion, guarding against `None`, `complex`, `NaN`, `inf`:
- `_sf(value)` in `multi_model_valuation.py`
- `to_float(value, default)` in `empresa.py`
- `to_optional_float(value)` in `empresa.py`

### Defensive dict access
Instead of direct key access, the pattern `(financials.get("datos_empresa") or {}).get("key")` is used throughout to handle `None` values nested in the result dict.

### Concurrent API prefetching
`dcf_core/DCF_Main.py` uses `ThreadPoolExecutor` to fire all external API calls in parallel (yfinance properties + FMP endpoints). A second parallel stage runs `analizar_empresa` and the news+AI pipeline concurrently.

### Django cache for expensive calls
Views wrap the expensive `ejecutar_dcf()` call with Django's cache framework (`cache.get` / `cache.set`) with a 10-minute TTL. Business cycle and ticker strip data are also cached.

### PRG (Post/Redirect/Get) pattern
`dcf_view` uses POST → redirect to GET with query params to avoid form resubmission on browser refresh.

### Graceful degradation
All external API calls are wrapped in `try/except`. Failures are caught silently or stored in `*_error` keys in the result dict, never raising to the user. The UI displays partial results with error banners rather than crashing.

### Dataclasses for API data
`dcf_core/fmp.py` uses `@dataclass(frozen=True)` for structured API response types (`FCFEntry`, `FMPDerivedMetrics`, `FMPSearchResult`, `FMPNewsItem`).

### Weighted consensus
`multi_model_valuation.py` calculates a weighted consensus price from up to 9 models. Weights come from a `WEIGHTS` matrix keyed by company stage (1–6). Models not applicable (negative input, missing data) are excluded and their weights redistributed proportionally.

---

## Error Handling Approach

### Custom exceptions
Each external client defines its own exception class:
- `FMPClientError(RuntimeError)` — Financial Modeling Prep errors
- `AISummaryError(RuntimeError)` — Groq/HuggingFace AI summary errors
- `MarketauxError` — Marketaux news errors
- `FinnhubError` — Finnhub news errors
- `_ModelUnavailableError(AISummaryError)` — private, specific HuggingFace model unavailability

### Catch-and-continue in views
In `dcf_view` and `comparar_view`, exceptions from `_cached_ejecutar_dcf` are caught and stored in the `error` template context variable. The page renders with the error message instead of raising a 500.

### Catch-and-continue in core modules
In `DCF_Main.py` and `empresa.py`, exceptions from individual pipeline steps (scenarios, sensitivity table, price history, technical analysis) are silently caught with `except Exception: resultado["key"] = None`. This prevents any single failure from aborting the full analysis.

### API key masking
`empresa.py` and `ai_summary.py` scrub `apikey=` and `token=` values from error messages before surfacing them to the UI (`_limpiar_mensaje_api`, `_sanitize`).

### Return `None` vs raise
Functions in `dcf_core` return `None` (or dicts with `"aplicable": False`) when data is insufficient rather than raising. Exceptions are only raised for truly unrecoverable conditions (missing API key in `FMPClient.__init__`, invalid input that cannot be defaulted).

### Django HTTP error responses
Views return explicit `HttpResponse(..., status=400/404/500)` for invalid inputs or missing data, rather than raising Django exceptions.
