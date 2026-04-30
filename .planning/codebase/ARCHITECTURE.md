# Architecture

## Overview

Proyecto DCF is a Django 5.2 web application that performs stock valuation analysis using the Discounted Cash Flow (DCF) method and a suite of complementary valuation models. It fetches financial data from multiple external APIs (yfinance, Financial Modeling Prep, FRED, Marketaux, Finnhub), computes intrinsic values, detects the company life-cycle stage, and generates AI-powered news sentiment summaries via Groq (primary) and Hugging Face (fallback). The app is deployable on Render (Heroku-compatible via Procfile + gunicorn + whitenoise).

## Patterns & Paradigms

- **Django MVT** — Models, Views, Templates; no Django REST Framework; all responses are either Django-rendered HTML or `JsonResponse`.
- **Separation of concerns via packages** — `dcf_app` is the Django application layer (views, models, templates, URLs); `dcf_core` is a pure-Python financial computation and data-access library with no Django imports, making it independently testable.
- **Stateless view functions** — All views are function-based (no class-based views). Query parameters drive analysis; POST submits redirect to GET to prevent re-submission.
- **Result dict as the primary data transfer object** — `analizar_empresa()` and `ejecutar_dcf()` return a large plain `dict` that flows all the way from `dcf_core` to Django templates without intermediate serialization objects.
- **Concurrent prefetching** — `DCF_Main.py` uses `concurrent.futures.ThreadPoolExecutor` to parallelize all external API calls (yfinance × 8 properties + FMP × 2 endpoints) before computation begins, reducing total latency.
- **Per-request in-memory cache** — Django's `cache` framework (default: in-process LocMemCache) stores DCF results and the ticker strip for 10 minutes and 5 minutes respectively, keyed by ticker symbol. No persistent cache backend is configured; on Render the process-level cache is ephemeral.
- **Graceful degradation** — Every external call is wrapped in `try/except`; failures fall back to alternative data sources or return `None`/empty results rather than raising to the user.
- **Frozen dataclasses for API responses** — FMP, Marketaux, and Finnhub each define `@dataclass(frozen=True)` value objects (`FCFEntry`, `FMPDerivedMetrics`, `MarketauxNewsItem`, `FinnhubNewsItem`) to ensure immutable data contracts between the API layer and the computation layer.

## Layer Structure

```
Browser / HTTP Client
        |
  Django URL Router  (Proyecto_DCF/urls.py)
        |
  dcf_app Views  (dcf_app/views.py)
        |  calls
  dcf_core.DCF_Main.ejecutar_dcf()  — orchestration entry point
       /              \
dcf_core.empresa      External Data Layer
.analizar_empresa()        |
       |             ┌─────┴──────────────────────────┐
  dcf_core.finanzas  │  dcf_core.fmp  (FMP REST API)  │
  (math/finance)     │  yfinance (Yahoo Finance)       │
       |             │  dcf_core.marketaux (news)      │
  dcf_core           │  dcf_core.finnhub  (news)       │
  .multi_model_      │  dcf_core.finanzas.obtener_     │
   valuation         │    tasa_libre_riesgo (FRED API) │
  .company_stage     └─────────────────────────────────┘
  .business_cycle
        |
  Django ORM (SQLite / Postgres)
  AnalysisRecord, WatchlistItem
        |
  Django Templates  (dcf_app/templates/)
        |
  Browser
```

Key layers:
1. **Presentation** — Django templates receive the `resultado` dict and `multi_model` dict, rendered server-side with no SPA framework. Chart data is injected as JSON into `<script>` tags for Chart.js and TradingView widgets.
2. **View / Orchestration** — `dcf_app/views.py` handles HTTP, caching, DB persistence (`AnalysisRecord`), and coordinates calls to `dcf_core`.
3. **Computation** — `dcf_core/` performs financial math and aggregation; never touches Django.
4. **Data Access** — Thin client wrappers in `dcf_core/fmp.py`, `finnhub.py`, `marketaux.py`, and direct `yfinance`/`requests` calls in `empresa.py` and `finanzas.py`.
5. **AI Layer** — `dcf_core/ai_summary.py` calls Groq (primary) → Hugging Face (fallback) with structured JSON prompts; encapsulated behind `generar_analisis_sentimiento()`.
6. **Persistence** — `AnalysisRecord` stores a snapshot of each analysis run. `WatchlistItem` stores user-bookmarked tickers. Both use SQLite locally and can switch to Postgres via `DATABASE_URL`.

## Data Flow

### Main DCF Analysis (GET `/app/?ticker=AAPL`)

1. `dcf_view` checks Django in-memory cache for `dcf_result_auto_{ticker}`.
2. On miss, calls `ejecutar_dcf(ticker, "auto", "auto")` in `DCF_Main.py`.
3. `DCF_Main` creates a `yf.Ticker` object and fires a `ThreadPoolExecutor` with 10 parallel tasks:
   - 8 yfinance property prefetches (info, cashflow, financials, balance_sheet, history×3, news)
   - 2 FMP API calls (FCF history, income/balance statements for tax rate + cost of debt)
4. After prefetch, `DCF_Main` selects the FCF data source (FMP preferred, yfinance fallback) and computes growth rates via `calcular_crecimientos()`.
5. A second `ThreadPoolExecutor(max_workers=2)` runs `analizar_empresa()` (core DCF + ratios + technicals) and `_pipeline_noticias()` (news fetch + AI summary) in parallel.
6. `analizar_empresa()` in `empresa.py` computes: WACC (via CAPM), FCF projection, intrinsic value per share, Graham filters, technical indicators (SMA, RSI), dividends.
7. Back in `DCF_Main`, escenarios (bear/base/bull) and sensitivity table are appended to the result dict.
8. Historical price data (5y) is appended.
9. Result dict is returned, cached, and passed to the view.
10. View calls `detect_company_stage()` and `run_all_models()` on the cached result.
11. View persists an `AnalysisRecord` (deduplication within 5-minute window).
12. Context dict is rendered through `dcf_app/index.html`.

### Multi-Model Valuation Flow

`run_all_models()` in `multi_model_valuation.py` receives the already-computed `resultado` dict (no additional API calls) and runs 10–11 models:
- DCF (reuse from main result)
- Reverse DCF (scipy.optimize.brentq solver)
- P/E Trailing, P/S, P/Gross Profit, P/FCF Trailing, Forward P/E, Forward P/FCF
- TAM-assisted (heuristic sector/stage penetration model)
- Forward Earnings Discounted (Schwab-style PEG-CAPM hybrid)
- Liquidation Value (Graham Net-Net)
- Altman Z-Score (informational, not in consensus)

Weights are selected from a `WEIGHTS[stage]` matrix (6 stages × 11 models) and renormalized to active models before computing the consensus weighted average price.

## Key Design Decisions

1. **FMP preferred over yfinance for FCF** — FMP provides cleaner annual FCF data with explicit `calendarYear`. yfinance is the fallback (less reliable for non-US tickers). Tax rate and cost of debt are computed from FMP income/balance statements when available.

2. **Concurrent I/O before computation** — All network calls are batched upfront in `_prefetch_concurrent()` rather than interleaved with computation, reducing perceived latency. The design accepts a fixed 20-second max timeout for the prefetch phase.

3. **Conservative growth selection** — When two growth methods (CAGR vs. year-over-year average) diverge, `seleccionar_metodo_crecimiento()` picks the one closest to zero (most conservative), reducing overvaluation bias.

4. **Stage-aware valuation** — The `company_stage.py` scoring system (9 signals, 6 stages) gates which valuation models carry weight in the multi-model consensus, following Brian Feroldi's "Valuation by Stage" framework. This prevents e.g. DCF dominating for pre-profit companies.

5. **No client-side state management** — All analysis results travel as Django template context. Pagination of news items, chart data, and watchlist status are handled via JSON API endpoints called from inline JavaScript, keeping the frontend simple.

6. **PDF and Excel export via cached data** — `dcf_pdf_view` and `dcf_excel_view` both call `_cached_ejecutar_dcf()`, so export requests reuse the in-memory cached result rather than re-fetching external data.

7. **AI sentiment with dual-provider fallback** — Groq (llama-3.3-70b) is attempted first as the primary LLM for structured JSON sentiment output (`score`, `resumen`, `temas`). Hugging Face (zephyr-7b-beta → BART) is the fallback with automatic chunking for large news sets.

8. **SQLite default, Postgres on Render** — Settings detect `DATABASE_URL` env var and swap to `dj_database_url` config automatically, enabling zero-config local development.
