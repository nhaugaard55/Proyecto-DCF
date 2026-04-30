# Integrations

## External APIs

### Yahoo Finance (yfinance)
- **Library:** `yfinance 0.2.66`
- **Auth:** None (no API key required; uses Yahoo Finance public endpoints internally)
- **Usage:** Primary source for real-time stock price, beta, EPS, P/E ratios, revenue, dividends, shares outstanding, balance sheet, income statement, cash flow statement, and company news
- **Endpoints (internal to yfinance):** `https://query1.finance.yahoo.com/` and `https://query2.finance.yahoo.com/`
- **Also used directly (search):** `https://query1.finance.yahoo.com/v1/finance/search` — fallback company search when FMP key is absent

### Financial Modeling Prep (FMP)
- **Auth:** `FMP_API_KEY` environment variable
- **Base URL:** `https://financialmodelingprep.com`
- **Endpoints used:**
  - `GET /api/v3/search` — company search by ticker or name
  - `GET /stable/cash-flow-statement` (fallback: `GET /api/v3/cash-flow-statement/{ticker}`) — historical FCF
  - `GET /stable/income-statement` — income statements (tax rate, interest expense)
  - `GET /stable/balance-sheet-statement` — balance sheets (debt, cash)
  - `GET /api/v3/profile/{ticker}` — company profile (sector, industry)
  - `GET /api/v3/stock_news` — recent news articles
- **Usage:** FCF history for DCF, derived financial metrics (effective tax rate, cost of debt), sector/industry lookup, news fallback

### Finnhub
- **Auth:** `FINNHUB_API_KEY` environment variable
- **Endpoint:** `GET https://finnhub.io/api/v1/company-news`
- **Usage:** Company news (secondary news source, fills gap after Marketaux)

### Marketaux
- **Auth:** `MARKETAUX_API_KEY` environment variable
- **Endpoint:** `GET https://api.marketaux.com/v1/news/all`
- **Usage:** Company news (primary news source, queried first before Finnhub and yfinance)

### FRED — Federal Reserve Bank of St. Louis
- **Auth:** `FRED_API_KEY` environment variable (default key hardcoded as fallback: `03b0d61b2efbea3313f92d4d117af8df`)
- **Base URL:** `https://api.stlouisfed.org/fred/series/observations`
- **Series queried:**
  - `DGS10` — 10-year Treasury yield (risk-free rate for WACC/CAPM)
  - `T10Y2Y` — 10Y/2Y yield spread (yield curve inversion signal)
  - `UNRATE` — US unemployment rate
  - `CPIAUCSL` — CPI (inflation)
- **Usage:** Risk-free rate for WACC calculation; macro indicators for economic cycle phase detection

## Third-party Services

### Groq (LLM API)
- **Auth:** `GROQ_API_KEY` environment variable
- **Endpoint:** `POST https://api.groq.com/openai/v1/chat/completions`
- **Model:** `llama-3.3-70b-versatile` (overridable via `GROQ_SUMMARY_MODEL`)
- **Usage:** Primary AI sentiment analysis engine — generates structured JSON with a numerical sentiment score (-5 to +5), a Spanish-language narrative summary, and key topics from recent news

### Hugging Face Inference API
- **Auth:** `HUGGINGFACE_API_TOKEN` environment variable
- **Base URL:** `https://router.huggingface.co/hf-inference/models`
- **Models used:**
  - `HuggingFaceH4/zephyr-7b-beta` — primary instruction-following model for news summaries
  - `facebook/bart-large-cnn` — seq2seq fallback summarizer
  - `Helsinki-NLP/opus-mt-en-es` — English-to-Spanish translation (post-processing step)
- **Usage:** Fallback AI sentiment summary when Groq is unavailable or fails; also supports hierarchical block-based summarization for large news sets

### Render (hosting)
- **Indicator:** `ALLOWED_HOSTS` includes `.onrender.com`; `CSRF_TRUSTED_ORIGINS` defaults to `https://proyecto-dcf.onrender.com`
- **Usage:** Production hosting platform; app deployed as a Gunicorn web service via `Procfile`

## Data Sources

### Sector ETFs via yfinance (business cycle detection)
Historical price data for 10 ETFs fetched via `yfinance` to compute sector rotation signals:
- `XLK` (Technology), `XLY` (Consumer Disc.), `XLI` (Industrials), `XLB` (Materials), `XLE` (Energy), `XLP` (Consumer Staples), `XLU` (Utilities), `XLF` (Financials), `IYZ` (Telecom), `XLV` (Healthcare)

### Static local company index (offline fallback)
- Hardcoded list of 31 large-cap tickers (AAPL, MSFT, AMZN, GOOGL, etc.) used as last-resort company search when both FMP and Yahoo Finance Search are unavailable

### Sector valuation ratios (hardcoded)
- Internal `_SECTOR_RATIOS` dict in `multi_model_valuation.py` mapping 10 GICS sectors to reference P/E, P/S, P/GP, P/FCF, and Forward P/E multiples used across valuation models

## Authentication Providers
- None. The application has no user authentication or login system. Django's `AUTH_PASSWORD_VALIDATORS` are configured but no login views or user accounts exist in the codebase.

## Other Integrations

### Google Fonts CDN
- **URL:** `https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700`
- **Usage:** Loads the Inter typeface for the UI

### Bootstrap CDN (jsDelivr)
- **URL:** `https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/...`
- **Usage:** CSS framework and JS bundle (modals, dropdowns, tooltips)

### Chart.js CDN (jsDelivr)
- **URL:** `https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js`
- **Usage:** All interactive charts in the analysis view (FCF history, FCF projections, scenario bar charts)
