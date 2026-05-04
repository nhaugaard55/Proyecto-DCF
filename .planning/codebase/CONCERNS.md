# Concerns & Tech Debt

## Known Issues

### 1. API keys hardcodeadas como fallback en código fuente
- **`dcf_core/finanzas.py` línea 9**: `_FRED_API_KEY_DEFAULT = "03b0d61b2efbea3313f92d4d117af8df"` — una clave FRED real está hardcodeada directamente en el código. Si esta clave se rota o expira, el fallback silenciosamente dejará de funcionar.
- **`dcf_core/business_cycle.py` línea 17**: idéntico problema con `_FRED_API_KEY_DEFAULT`.
- **`dcf_core/empresa.py` línea 479**: `cost_of_debt_info = to_float(info.get("yield"), 0.05)` — usa el campo `yield` de yfinance como proxy del costo de la deuda, pero `yield` es el dividend yield de bonos, no el costo de la deuda corporativa. Este campo es conceptualmente incorrecto para la mayoría de empresas de equity.

### 2. Archivo `.env` con secretos reales en el repositorio
- El `.gitignore` incluye `.env`, pero el archivo `.env` contiene en este momento claves API reales (`FMP_API_KEY`, `FINNHUB_API_KEY`, `MARKETAUX_API_KEY`, `HUGGINGFACE_API_TOKEN`, `GROQ_API_KEY`, `Openai_API_KEY`). Si alguna vez se commitea accidentalmente o el `.gitignore` falla, las claves quedan expuestas.

### 3. `exportar.py` es código muerto
- `dcf_core/exportar.py` define una sola función `exportar_resultado()` que escribe en un archivo de texto local. No está importada ni usada desde ningún otro módulo. Nunca se expone como endpoint ni se conecta al flujo principal.

### 4. Nombre de variable inconsistente para OpenAI
- En `.env`: `Openai_API_KEY` (mayúscula mixta, no estándar). En `.env.example` figura como `OPENAI_API_KEY`. No hay código en el proyecto que lea ninguna de las dos, así que OpenAI no está integrado aunque la clave existe en el `.env`.

### 5. Falla silenciosa en `_generate_ai_summary` (duplicación de lógica de filtrado)
- En `dcf_core/empresa.py` líneas 300-311, `relevantes_contenido` filtra noticias usando exactamente el mismo predicado que `relevantes_titulo`, haciendo que el segundo bloque nunca añada noticias adicionales (el condicional `or` en el segundo filter repite la condición del primero). La segunda rama es letra muerta.

### 6. `AnalysisRecord` no tiene índice en `ticker` sólo
- `dcf_app/models.py`: el índice compuesto es `(ticker, created_at)`. La consulta de deduplicación en `_guardar_analisis` (views.py línea 174) filtra por `ticker`, `metodo`, `fuente_utilizada`, `valor_intrinseco`, `precio_actual`, `diferencia_pct`, y `created_at__gte`. El índice existente ayuda parcialmente, pero no es un índice sobre `ticker` solo para la vista de historial reciente.

---

## Tech Debt

### 1. `dcf_view` es una función monolítica de 120+ líneas
- `dcf_app/views.py`: la función `dcf_view` mezcla resolución de ticker, ejecución del análisis, detección de etapa, multi-model valuation, paginación de noticias, consulta al historial, y construcción del contexto. Debería descomponerse en helpers o una clase-based view.

### 2. `analizar_empresa` tiene ~450 líneas y hace demasiado
- `dcf_core/empresa.py`: una única función obtiene datos del balance, calcula métricas financieras, detecta EPS, calcula WACC, proyecta FCF, calcula valor intrínseco, construye los filtros de pantalla, genera noticias y análisis técnico. Cualquier cambio en una parte afecta a todo el flujo.

### 3. Duplicación de la lógica de cálculo de tasa impositiva y costo de deuda
- `dcf_core/DCF_Main.py` implementa `_obtener_metricas_yfinance()` que extrae tax rate y cost of debt de los estados financieros de yfinance. `dcf_core/fmp.py` implementa `obtener_metricas_financieras()` que hace lo mismo desde FMP. La lógica de averaging y sanitización se repite en ambos, con pequeñas variaciones. No existe una capa de abstracción compartida.

### 4. La caché usa el backend por defecto de Django (in-memory local)
- `settings.py` no configura `CACHES`, por lo que usa `LocMemCache`. En producción con múltiples workers de Gunicorn, cada proceso tiene su propia caché independiente. Esto significa que la caché de 10 minutos por ticker no se comparte entre workers, produciendo llamadas redundantes a las APIs externas.

### 5. `recent_records_queryset` carga hasta 25 registros en cada request a `/app/`
- `dcf_app/views.py` línea 368-369: `AnalysisRecord.objects.all()` se evalúa en cada request de la página principal, incluso cuando el usuario no buscó ningún ticker. Tampoco filtra por ningún criterio más allá del orden.

### 6. `_local_company_index()` con `lru_cache` contiene sólo 31 empresas hardcodeadas
- `dcf_core/search.py`: el índice local de fallback tiene 31 empresas de US hardcodeadas. Si FMP y Yahoo Finance fallan, la búsqueda de cualquier ticker que no esté en esa lista devuelve vacío, sin ningún feedback al usuario.

### 7. El campo `metodo` en `AnalysisRecord` sólo tiene 2 opciones pero usa `max_length=2`
- `dcf_app/models.py`: el campo `METODO_CAGR = "1"` y `METODO_PROMEDIO = "2"` usan strings de un carácter pero `max_length=2`. Esto no genera errores pero evidencia que el modelo fue pensado para expandirse y nunca se completó (los choices no tienen "auto" como opción real, aunque se salva como `AnalysisRecord.METODO_CAGR` cuando viene `None`).

### 8. `proyectar_fcf` tiene lógica incorrecta para FCF negativo en años posteriores al primero
- `dcf_core/finanzas.py` líneas 57-63: para `i > 0`, si `prev > 0` proyecta con crecimiento compuesto normal, pero si `prev <= 0`, calcula `(prev - prev_prev) * (1 + tasa) + prev`. Este cálculo produce resultados no intuitivos cuando el FCF oscila entre positivo y negativo a lo largo de las proyecciones, porque `prev_prev` referencia `fcf_actual` en `i == 1`, no el valor del año anterior al anterior.

---

## Security Concerns

### 1. Clave secreta de Django con fallback inseguro en producción
- `settings.py` línea 37-40: `SECRET_KEY` tiene un fallback hardcodeado `'django-insecure-...'`. Si la variable de entorno `SECRET_KEY` no está definida en producción, Django usa esa clave pública. Aunque el nombre incluye "insecure", este fallback no debería existir: debería fallar explícitamente si la variable no está definida.

### 2. Claves API en `.env` sin rotación documentada
- El `.env` contiene 5 claves de APIs de terceros. No existe en el proyecto ningún proceso ni documentación para rotarlas. Si alguna se compromete, no hay mecanismo de revocación ni alertas.

### 3. No hay rate limiting en ningún endpoint de la app
- Los endpoints `/api/search_companies/`, `/app/` (búsqueda con ticker), `/dcf/pdf/`, `/dcf/excel/`, y `/api/business-cycle/` no tienen rate limiting. Un actor malicioso puede hacer polling masivo para agotar las cuotas de FMP, Finnhub, Marketaux y Groq en cuestión de minutos.

### 4. `dcf_pdf_view` y `dcf_excel_view` no validan el ticker contra un patrón estricto
- `views.py` líneas 423 y 585: hacen `.strip().upper()` pero no aplican el regex `_SYMBOL_PATTERN` que sí usa `_resolver_ticker`. Un ticker arbitrariamente largo o con caracteres especiales puede ser pasado a todas las APIs externas.

### 5. `Content-Disposition` en respuestas PDF/Excel no sanitiza el ticker
- `views.py` líneas 447 y 717: `f'attachment; filename="DCF_{ticker}.xlsx"'` y `f'attachment; filename="DCF_{ticker}.pdf"'`. Si el ticker contiene `"`, podría producir un header malformado (HTTP response splitting). El riesgo es bajo dado que el ticker viene de `.strip().upper()`, pero un `"` sí puede pasar porque `_SYMBOL_PATTERN` admite más caracteres que letras y números.

### 6. La watchlist no tiene autenticación
- `dcf_app/views.py`: `watchlist_toggle` (POST) y `watchlist_view` son accesibles sin ningún usuario autenticado. La watchlist es única (no por usuario), por lo que cualquier persona que acceda a la misma instancia puede agregar o borrar elementos de la watchlist de cualquier otro.

---

## Performance Concerns

### 1. Hasta 10 llamadas paralelas con `ThreadPoolExecutor` en cada análisis
- `dcf_core/DCF_Main.py` línea 108: `ThreadPoolExecutor(max_workers=len(tasks))` crea hasta 10 threads por request. En producción con múltiples usuarios simultáneos, esto puede generar cientos de threads activos compitiendo por las mismas APIs externas.

### 2. La caché de análisis DCF no se comparte entre workers
- Como se menciona en Tech Debt #4, `LocMemCache` implica que con Gunicorn en producción, el mismo ticker puede ser analizado múltiples veces en paralelo por distintos workers, cada uno haciendo sus propias llamadas a todas las APIs.

### 3. `yf.history(period="5y")` se llama dos veces para el mismo ticker
- `dcf_core/DCF_Main.py`: el pre-fetch paralelo llama `_yf_history_5y()` (línea 64) para cachear el resultado en el objeto `yf.Ticker`. Luego, `ejecutar_dcf` vuelve a llamar `empresa_yf.history(period="5y")` en la línea 443 directamente. Aunque yfinance debería usar su caché interna, la doble llamada no es explícita ni garantizada.

### 4. `ticker_strip_view` descarga 2 días de datos para 10 tickers en cada request no cacheado
- `views.py` línea 253: `yf.download(symbols, period="2d", ...)` descarga datos para los 10 tickers en un bulk. La caché de 5 minutos ayuda, pero si expira durante tráfico pico, puede generar una cola de requests simultáneos al mismo endpoint.

### 5. El análisis de ciclo económico (`business_cycle_view`) hace 6 llamadas a FRED + 1 a yfinance
- `dcf_core/business_cycle.py`: `_get_macro_signals()` hace 5 llamadas separadas a la API de FRED (no paralelizadas). `_get_sector_rotation()` hace una llamada `yf.download()` con 11 tickers. Todas son síncronas y secuenciales dentro de la función, salvo la descarga batch de yfinance.

### 6. `AnalysisRecord.objects.all()` sin límite antes del slice
- `views.py` línea 368-369: `AnalysisRecord.objects.all()` evalúa la queryset completa antes de slicearla. Debería ser `AnalysisRecord.objects.all()[:RECENT_HISTORY_FETCH_LIMIT]` directamente para que el LIMIT llegue a la base de datos.

---

## Missing or Incomplete Features

### 1. `exportar.py` nunca fue integrado
- `dcf_core/exportar.py` tiene una función stub que escribe un archivo de texto. No está conectada a ningún endpoint ni view. No está claro si es un vestigio o una feature planeada.

### 2. La watchlist no tiene datos financieros en tiempo real
- `dcf_app/views.py`: `watchlist_view` sólo lista los tickers guardados sin precio actual, variación del día, ni valor intrínseco calculado. Un usuario que agrega empresas a la watchlist no puede ver su estado sin hacer click en cada una individualmente.

### 3. No hay página de error 404 ni 500 personalizada
- No existe ningún template `404.html` ni `500.html` en el proyecto. En producción con `DEBUG=False`, Django mostrará sus páginas de error genéricas en inglés.

### 4. La comparación (`comparar_view`) ejecuta dos análisis completos secuencialmente
- `dcf_app/views.py` líneas 552-563: los dos DCFs se ejecutan en secuencia, no en paralelo. Si el segundo ticker tarda, el usuario espera la suma de ambos tiempos. Estos podrían paralelizarse con `ThreadPoolExecutor` igual que el prefetch interno.

### 5. El `.env.example` no documenta las variables de Groq ni de Finnhub
- `.env.example` menciona `HUGGINGFACE_API_TOKEN` y `OPENAI_API_KEY` pero no incluye `GROQ_API_KEY`, `FINNHUB_API_KEY`, ni `MARKETAUX_API_KEY`. Un desarrollador nuevo que copie el ejemplo queda sin las variables necesarias para que funcionen las noticias y el análisis de sentimiento.

### 6. No hay tests para las views, los modelos de Django ni la mayoría de módulos del core
- `dcf_app/tests.py` tiene 4 clases con tests unitarios enfocados en `multi_model_valuation`, `company_stage`, y `seleccionar_metodo_crecimiento`. No hay tests para:
  - Ninguna view (dcf_view, comparar_view, watchlist_toggle, etc.)
  - `empresa.py` (analizar_empresa, calcular_analisis_tecnico)
  - `business_cycle.py`
  - `ai_summary.py`
  - `search.py`
  - `marketaux.py`, `finnhub.py`
  - Los modelos de Django y sus métodos

### 7. No hay manejo de errores cuando `precio_actual = 0` en el cálculo de escenarios
- `dcf_core/DCF_Main.py` línea 419: `precio = resultado.get("precio_actual") or 0.0`. Si el precio es 0 (ticker sin precio de cierre disponible), `calcular_escenarios` recibe `precio=0.0` y la `diferencia_pct` calcula una división por cero que está protegida con `if precio else None`, pero el escenario `estado` queda como `None` en lugar de un mensaje claro.

---

## Recommendations

1. **Eliminar los fallbacks de claves hardcodeadas**: `_FRED_API_KEY_DEFAULT` en `finanzas.py` y `business_cycle.py` deberían ser eliminados. Si la variable de entorno no está definida, la función debería retornar `None` o un error descriptivo, no usar una clave que podría expirar silenciosamente.

2. **Agregar Redis o Memcached como backend de caché en producción**: reemplazar `LocMemCache` con un backend compartido para que la caché de 10 minutos por ticker funcione correctamente con múltiples workers de Gunicorn en Render.

3. **Agregar rate limiting**: usar `django-ratelimit` o similar en los endpoints que disparan llamadas a APIs externas, especialmente `/app/` (búsqueda), `/api/search_companies/`, y `/api/business-cycle/`.

4. **Paralelizar los dos análisis en `comparar_view`**: con `ThreadPoolExecutor(max_workers=2)`, igual al patrón ya usado en `ejecutar_dcf`, el tiempo de la vista de comparación se reduciría a la mitad.

5. **Completar `.env.example`**: agregar `GROQ_API_KEY`, `FINNHUB_API_KEY`, y `MARKETAUX_API_KEY` con comentarios descriptivos para que el onboarding sea completo.

6. **Eliminar o integrar `exportar.py`**: si la exportación a texto no se va a usar, eliminar el archivo. Si se quiere mantener, conectarlo a un endpoint o eliminarlo para no crear deuda cognitiva.

7. **Agregar autenticación a la watchlist**: aunque la app actualmente no tiene sistema de usuarios, la watchlist debería ser al menos protegida con un token de sesión o CSRF más restrictivo si se quiere evitar que actores externos la manipulen.

8. **Crear templates de error personalizados**: agregar `dcf_app/templates/404.html` y `dcf_app/templates/500.html` para que los errores en producción se muestren con el mismo diseño de la app y en español.

9. **Validar y sanitizar el ticker antes de usarlo en nombres de archivo**: en `dcf_pdf_view` y `dcf_excel_view`, aplicar el mismo `_SYMBOL_PATTERN` que usa `_resolver_ticker` para asegurar que el ticker sólo contenga caracteres válidos antes de usarlo en el `Content-Disposition`.

10. **Corregir la lógica duplicada en `_generate_ai_summary`**: el segundo bloque de filtrado de noticias relevantes (`relevantes_contenido`) usa el mismo predicado que el primero y nunca agrega noticias adicionales. Debería revisar `resumen` además de `titulo` para ser útil.
