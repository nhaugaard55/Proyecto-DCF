# Tech Stack

## Languages
- **Python 3.12.3** — runtime declared in `runtime.txt`; all backend logic
- **HTML/CSS/JavaScript** — frontend templates (Django template engine)

## Frameworks & Libraries

### Backend (Python)
- **Django 5.2.6** — web framework; views, models, URL routing, template rendering, in-memory cache
- **gunicorn 21.2.0** — WSGI production server (declared in `Procfile`)
- **whitenoise 6.6.0** — static file serving with compression (`CompressedManifestStaticFilesStorage`)
- **dj-database-url 2.2.0** — parses `DATABASE_URL` environment variable into Django `DATABASES` config
- **python-dotenv 1.0.1** — loads `.env` file into environment variables
- **asgiref 3.9.2** — ASGI/sync bridge (Django dependency)
- **sqlparse 0.5.3** — SQL formatting (Django dependency)

### Data & Finance
- **yfinance 0.2.66** — primary source for stock price, balance sheet, cash flow, income statement, and news via Yahoo Finance
- **pandas 2.3.2** — DataFrame operations on financial statements
- **numpy 2.3.3** — numerical computation support
- **scipy 1.17.1** — `scipy.optimize.brentq` used in Reverse DCF solver
- **requests 2.32.5** — all HTTP calls to external APIs (FRED, Groq, Hugging Face, Marketaux, Finnhub, FMP, Yahoo Finance Search)
- **curl_cffi 0.13.0** — cURL-based HTTP (yfinance dependency for anti-bot bypass)
- **multitasking 0.0.12** — async task management (yfinance dependency)
- **frozendict 2.4.6** — immutable dict (yfinance dependency)
- **websockets 15.0.1** — WebSocket support (yfinance dependency)
- **beautifulsoup4 4.13.5** + **soupsieve 2.8** — HTML parsing (yfinance/requests dependency)
- **protobuf 6.32.1** — protocol buffers (yfinance dependency)

### Export & Reporting
- **xhtml2pdf 0.2.15** — generates PDF reports from Django HTML templates (via `pisa`)
- **openpyxl 3.1.5** — generates Excel (`.xlsx`) export files with styled headers

### Utilities
- **python-dateutil 2.9.0** — robust date parsing
- **pytz 2025.2** / **tzdata 2025.2** — timezone support
- **certifi 2025.8.3** — SSL certificates
- **urllib3 2.5.0** — HTTP client (requests dependency)
- **cffi 2.0.0** / **pycparser 2.23** — C foreign function interface (curl_cffi dependency)
- **idna 3.10** — internationalized domain names (requests dependency)
- **charset-normalizer 3.4.3** — encoding detection (requests dependency)
- **six 1.17.0** — Python 2/3 compat shim (dependency chain)
- **platformdirs 4.4.0** — platform-specific directories (dependency chain)
- **typing_extensions 4.15.0** — backported type hints

### Frontend (CDN-loaded)
- **Bootstrap 5.3.2** — CSS framework and JS components (loaded from `cdn.jsdelivr.net`)
- **Chart.js 4.4.2** — interactive financial charts: FCF history, projections, scenario analysis (loaded from `cdn.jsdelivr.net`)
- **Google Fonts — Inter** (weights 400/500/600/700) — primary UI typeface

## Package Management
- **pip** with `requirements.txt` — pinned versions for all 34 dependencies
- No `pyproject.toml` or `setup.cfg`; no `poetry.lock` or `Pipfile`

## Build & Dev Tools
- **Django `manage.py`** — development server, migrations, static file collection
- **gunicorn** — production WSGI server launched via `Procfile` (Heroku/Render compatible)
- **whitenoise** — serves compressed/hashed static files without a reverse proxy
- **SQLite** (`db.sqlite3`) — default local database; overridden by `DATABASE_URL` in production (supports PostgreSQL via `dj-database-url`)
- **Django in-memory cache** — used in `views.py` to cache DCF results for 10 minutes per ticker

## Environment & Config
All sensitive values are injected via environment variables (loaded from `.env` in development via `python-dotenv`):

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django secret key |
| `DJANGO_DEBUG` | `"true"` enables debug mode |
| `ALLOWED_HOSTS` | Comma-separated allowed hostnames |
| `CSRF_TRUSTED_ORIGINS` | Trusted origins for CSRF |
| `DATABASE_URL` | Production database connection string |
| `DATABASE_SSL_REQUIRE` | Whether to enforce SSL on DB connection |
| `FMP_API_KEY` | Financial Modeling Prep API key |
| `FINNHUB_API_KEY` | Finnhub API key |
| `MARKETAUX_API_KEY` | Marketaux API key |
| `FRED_API_KEY` | FRED (St. Louis Fed) API key |
| `GROQ_API_KEY` | Groq LLM API key |
| `GROQ_SUMMARY_MODEL` | Override Groq model (default: `llama-3.3-70b-versatile`) |
| `HUGGINGFACE_API_TOKEN` | Hugging Face Inference API token |
| `HUGGINGFACE_SUMMARY_MODEL` | Override HF model (default: `HuggingFaceH4/zephyr-7b-beta`) |
| `HUGGINGFACE_SUMMARY_FALLBACK` | Fallback HF model (default: `facebook/bart-large-cnn`) |
| `HUGGINGFACE_TRANSLATION_MODEL` | Translation model (default: `Helsinki-NLP/opus-mt-en-es`) |
| `HUGGINGFACE_ALWAYS_TRANSLATE` | Force translation step |
