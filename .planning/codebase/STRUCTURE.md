# Project Structure

## Directory Layout

```
Proyecto DCF - 1.4/
├── manage.py                    # Django CLI entry point
├── Procfile                     # gunicorn deployment for Render/Heroku
├── requirements.txt             # Python dependencies
├── runtime.txt                  # Python version pin
├── .env                         # Local secrets (gitignored)
├── .env.example                 # Template for .env
├── .gitignore
├── db.sqlite3                   # Local development database
│
├── Proyecto_DCF/                # Django project configuration package
│   ├── settings.py              # Settings: DB, auth, static files, env vars
│   ├── urls.py                  # Root URL dispatcher
│   ├── wsgi.py                  # WSGI entry point (gunicorn)
│   └── asgi.py                  # ASGI entry point (unused in prod)
│
├── dcf_app/                     # Django application: HTTP layer + templates
│   ├── views.py                 # All view functions (main analysis, PDF, Excel, watchlist, compare)
│   ├── models.py                # AnalysisRecord, WatchlistItem
│   ├── urls.py                  # App-level URL patterns under /app/
│   ├── admin.py                 # Django Admin registrations
│   ├── apps.py                  # App config
│   ├── migrations/              # Database migrations
│   │   ├── 0001_initial.py
│   │   └── 0002_watchlist_item.py
│   └── templates/
│       ├── landing.html         # Public landing page (/)
│       └── dcf_app/
│           ├── base.html        # Base template (nav, ticker strip, footer)
│           ├── index.html       # Main analysis page (/app/)
│           ├── comparar.html    # Side-by-side company comparison (/app/comparar/)
│           ├── watchlist.html   # Watchlist page (/app/watchlist/)
│           ├── pdf_report.html  # PDF export template
│           └── components/
│               ├── business_cycle_chart.html  # Business cycle SVG widget
│               └── compare_metrics.html       # Comparison metrics partial
│
├── dcf_core/                    # Pure-Python financial library (no Django imports)
│   ├── DCF_Main.py              # Top-level orchestrator: ejecutar_dcf()
│   ├── empresa.py               # Core analysis: analizar_empresa(), news fetch, technicals
│   ├── finanzas.py              # Financial math: WACC, FCF projection, intrinsic value, scenarios
│   ├── multi_model_valuation.py # 10-model valuation engine + consensus + Altman Z-Score
│   ├── company_stage.py         # Life-cycle stage detection (6 stages, scoring algorithm)
│   ├── business_cycle.py        # Macro cycle detection (FRED + sector ETF rotation)
│   ├── fmp.py                   # Financial Modeling Prep API client + data models
│   ├── marketaux.py             # Marketaux news API client
│   ├── finnhub.py               # Finnhub news API client
│   ├── search.py                # Company search: FMP → Yahoo Finance → local fallback
│   ├── ai_summary.py            # Groq + Hugging Face sentiment analysis
│   ├── utils.py                 # Shared datetime parsing utilities
│   └── exportar.py              # Legacy text file exporter (unused by the web app)
│
├── staticfiles/                 # Collected static assets (Django admin CSS/JS)
│   └── admin/                   # Standard Django admin static files
│
└── .planning/                   # Project planning documents
    └── codebase/
        ├── ARCHITECTURE.md
        └── STRUCTURE.md
```

## Key Files

| File | Role |
|------|------|
| `dcf_core/DCF_Main.py` | Single public function `ejecutar_dcf()` — the orchestration entry point called by all views that need a full analysis. Manages parallel prefetch, source selection, parallelized DCF + news pipeline, and result assembly. |
| `dcf_core/empresa.py` | `analizar_empresa()` — the core financial computation function. Computes WACC, FCF projection, intrinsic value, Graham filters, technical indicators (SMA/RSI), and optionally fetches news + AI summary. |
| `dcf_core/finanzas.py` | Pure math functions: `calcular_wacc()`, `proyectar_fcf()`, `calcular_valor_intrinseco()`, `calcular_crecimientos()`, `calcular_escenarios()`, `calcular_tabla_sensibilidad()`. Shared constant `G_TERMINAL = 0.025`. Also fetches the 10-year Treasury rate from FRED. |
| `dcf_core/multi_model_valuation.py` | `run_all_models()` — runs 10 valuation models against the already-computed `resultado` dict, applies stage-weighted consensus, and returns detailed model breakdown including Altman Z-Score. |
| `dcf_core/company_stage.py` | `detect_company_stage()` — scoring system across 9 financial signals to classify a company into one of 6 life-cycle stages (Startup → Decline). Exposes `STAGE_META` dict consumed by views and multi-model engine. |
| `dcf_core/business_cycle.py` | `get_business_cycle_phase()` — combines 5 FRED macro indicators with 10-sector ETF relative performance to produce a cycle phase label, confidence score, and SVG marker coordinates. |
| `dcf_core/fmp.py` | `FMPClient` class wrapping Financial Modeling Prep REST API. Exposes `obtener_fcf_historico()`, `obtener_metricas_financieras()`, `obtener_sector_empresa()`. Defines frozen dataclasses `FCFEntry`, `FMPDerivedMetrics`, `FMPSearchResult`. |
| `dcf_core/ai_summary.py` | `generar_analisis_sentimiento()` — tries Groq LLM first, falls back to Hugging Face. Returns structured dict: `score` (−5 to +5), `label`, `color`, `resumen`, `temas`, `modelo`. |
| `dcf_core/search.py` | `search_companies()` — cascading company lookup: FMP API → Yahoo Finance `/v1/finance/search` → hardcoded local index of 30 major companies. |
| `dcf_app/views.py` | All HTTP view functions. Contains `dcf_view` (main page), `dcf_pdf_view`, `dcf_excel_view`, `business_cycle_view`, `comparar_view`, `watchlist_view`, `watchlist_toggle`, `watchlist_status`, `search_companies_view`, `ticker_strip_view`. |
| `dcf_app/models.py` | `AnalysisRecord` — persisted DCF snapshot with ticker, intrinsic value, current price, difference %, method, and data source. `WatchlistItem` — user-bookmarked ticker with company name and exchange. |
| `Proyecto_DCF/settings.py` | Django settings with environment variable-driven configuration for SECRET_KEY, DEBUG, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, DATABASE_URL, and STATICFILES_STORAGE (whitenoise). |
| `Proyecto_DCF/urls.py` | Root URL conf: `''` → landing, `api/ticker-strip/` → JSON endpoint, `app/` → includes `dcf_app.urls`. |

## Module Organization

### `dcf_core` internal dependency graph

```
DCF_Main
  ├── empresa  (analizar_empresa, _fetch_news, _generate_ai_summary)
  │     ├── finanzas  (WACC, FCF math)
  │     ├── fmp       (obtener_sector_empresa)
  │     ├── ai_summary (generar_analisis_sentimiento)
  │     ├── marketaux  (obtener_noticias_marketaux)
  │     ├── finnhub    (obtener_noticias_finnhub)
  │     └── utils      (parse_datetime_epoch)
  ├── finanzas  (calcular_crecimientos, calcular_escenarios, calcular_tabla_sensibilidad)
  └── fmp       (FCFEntry, FMPDerivedMetrics, obtener_fcf_historico, obtener_metricas_financieras)

multi_model_valuation
  ├── finanzas  (G_TERMINAL, proyectar_fcf, calcular_valor_intrinseco)
  └── company_stage  (STAGE_META — deferred import)

company_stage   (no dcf_core deps)
business_cycle  (no dcf_core deps — uses requests + yfinance directly)
search          ├── fmp  (FMPClient, FMPClientError, FMPSearchResult)
ai_summary      (no dcf_core deps — uses requests directly)
utils           (no deps)
exportar        (no deps — legacy stub)
```

### `dcf_app` internal dependencies

```
views.py
  ├── models.py               (AnalysisRecord, WatchlistItem)
  ├── dcf_core.DCF_Main       (ejecutar_dcf)
  ├── dcf_core.business_cycle (get_business_cycle_phase)
  ├── dcf_core.company_stage  (detect_company_stage, STAGE_META)
  ├── dcf_core.multi_model_valuation (run_all_models)
  └── dcf_core.search         (search_companies, CompanySearchResult)
```

## Entry Points

### HTTP entry points (all handled by `dcf_app/views.py`)

| URL pattern | View function | Method | Description |
|-------------|---------------|--------|-------------|
| `/` | `landing` | GET | Public landing page |
| `/api/ticker-strip/` | `ticker_strip_view` | GET | JSON: prices for 10 large-cap tickers (yfinance, 5-min cache) |
| `/app/` | `dcf_view` | GET/POST | Main DCF analysis page |
| `/app/dcf/pdf/` | `dcf_pdf_view` | GET | Download PDF report for a ticker |
| `/app/dcf/excel/` | `dcf_excel_view` | GET | Download Excel report for a ticker |
| `/app/watchlist/` | `watchlist_view` | GET | Watchlist page |
| `/app/watchlist/toggle/` | `watchlist_toggle` | POST | Add/remove ticker from watchlist (JSON) |
| `/app/watchlist/status/` | `watchlist_status` | GET | Check if ticker is in watchlist (JSON) |
| `/app/comparar/` | `comparar_view` | GET | Side-by-side DCF comparison of two tickers |
| `/app/api/search_companies/` | `search_companies_view` | GET | Autocomplete company search (JSON) |
| `/app/api/business-cycle/` | `business_cycle_view` | GET | Business cycle phase detection (JSON, 10-min cache) |

### Python entry points

- **Web server**: `Proyecto_DCF.wsgi:application` (gunicorn, as declared in `Procfile`)
- **Management**: `manage.py` (Django standard CLI: `runserver`, `migrate`, `collectstatic`)
- **Core analysis**: `dcf_core.DCF_Main.ejecutar_dcf(ticker, metodo, fuente)` — the sole public function called by all views requiring a full valuation.
- **Multi-model**: `dcf_core.multi_model_valuation.run_all_models(ticker, financials, stage, wacc)` — called after `ejecutar_dcf` with stage from `detect_company_stage`.
