# DCF Analyzer

## Variables de entorno

Copiá `.env.example` a `.env` y completá las claves necesarias según las fuentes que quieras habilitar.

- `DJANGO_DEBUG`: activa modo debug en desarrollo.
- `ALLOWED_HOSTS`: hosts permitidos por Django.
- `CSRF_TRUSTED_ORIGINS`: orígenes confiables para CSRF.
- `FMP_API_KEY`: clave de Financial Modeling Prep para estados financieros, búsqueda, noticias e insider trading.
- `FINNHUB_API_KEY`: clave de Finnhub para noticias e insider trading.
- `HUGGINGFACE_API_TOKEN`: token de Hugging Face para resúmenes o traducciones.
- `OPENAI_API_KEY`: clave de OpenAI si se habilitan integraciones IA que la usen.
