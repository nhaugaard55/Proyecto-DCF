"""Generates market sentiment summaries using Hugging Face Inference API."""

from __future__ import annotations

import math
import os
import re
from typing import Iterable, Mapping, Optional

import requests


class AISummaryError(RuntimeError):
    """Raised when the sentiment summary could not be generated."""


class _ModelUnavailableError(AISummaryError):
    """Raised when the requested Hugging Face model is not accessible."""

    def __init__(self, model_id: str, message: str) -> None:
        super().__init__(message)
        self.model_id = model_id


_DEFAULT_MODEL = "HuggingFaceH4/zephyr-7b-beta"
_FALLBACK_MODEL = "facebook/bart-large-cnn"
_TRANSLATION_MODEL = "Helsinki-NLP/opus-mt-en-es"
_HF_BASE_URL = "https://api-inference.huggingface.co/models"

# Mantener el prompt dentro de una ventana que el modelo pueda manejar.
_MAX_NOTICIAS_PROMPT = 8
_MAX_TITULO_CHARS = 140
_MAX_RESUMEN_CHARS = 320
_MAX_PROMPT_CHARS = 4200
_CTA_REGEX = re.compile(
    r"\b("
    r"haga\s+clic"
    r"|haga\s+click"
    r"|clic[ck]?\s+aquí"
    r"|click\s+here"
    r"|lea\s+más"
    r"|read\s+more"
    r"|ver\s+más"
    r"|aprenda\s+más"
    r"|descarg[ae]"
    r")\b",
    re.IGNORECASE,
)


def _sanitize(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"apikey=[^&\s]+", "apikey=****", text, flags=re.IGNORECASE)
    text = re.sub(r"token=[^&\s]+", "token=****", text, flags=re.IGNORECASE)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ****", text, flags=re.IGNORECASE)
    return text


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _limpiar_texto_noticia(texto: str) -> str:
    if not texto:
        return ""
    texto = re.sub(r"http\S+", "", texto)
    partes = _SENTENCE_SPLIT_RE.split(texto.strip())
    frases: list[str] = []
    for frase in partes:
        limpia = frase.strip()
        if not limpia:
            continue
        if _CTA_REGEX.search(limpia):
            continue
        frases.append(limpia)
    return " ".join(frases).strip()


def _compose_prompt(noticias: Iterable[Mapping[str, object]], idioma: str) -> str:
    noticias = list(noticias)
    if _MAX_NOTICIAS_PROMPT and len(noticias) > _MAX_NOTICIAS_PROMPT:
        noticias = noticias[:_MAX_NOTICIAS_PROMPT]
    empresa = str(noticias[0].get("empresa")) if noticias else "la compañía analizada"
    instruccion = (
        f"Eres un analista financiero hispanohablante. Analiza solo las noticias listadas sobre {empresa}. "
        f"Redacta en {idioma} un resumen narrativo y natural, con tono periodístico latino, que conecte los hechos mediante transiciones fluidas y mantenga un hilo conductor. "
        "Relaciona las noticias destacando vínculos causa-efecto o contrastes, e integra las ideas principales de cada artículo (qué ocurrió y por qué importa) con interpretaciones breves sobre su impacto en el sentimiento hacia la compañía. "
        "Escribe al menos tres frases enlazadas con conectores naturales (por ejemplo, 'además', 'sin embargo', 'mientras tanto', 'por su parte') y redacta con tus propias palabras, sin reproducir textualmente las notas. "
        "Evita enumeraciones rígidas, expresiones mecánicas o frases aisladas; escribe en un único párrafo coherente con conectores variados. "
        "Ignora frases promocionales o llamadas a la acción presentes en las noticias (por ejemplo, 'haga clic', 'lea más') y omite invitaciones a descargar o leer contenidos externos. "
        "Cierra con una frase que indique si el tono general resulta positivo, negativo o mixto. Sé conciso, evita redundancias y usa únicamente las frases necesarias, "
        "sin añadir información externa ni menciones a otras empresas."
    )
    partes: list[str] = []
    base_prompt_longitud = (
        len("<|system|>\n")
        + len("</s>\n")
        + len("<|user|>\nNoticias:\n")
        + len("\n\nResumen:<|assistant|>")
    )
    longitud_actual = base_prompt_longitud + len(instruccion)
    for indice, noticia in enumerate(noticias, start=1):
        titulo = str(noticia.get("titulo") or "").strip()
        if _MAX_TITULO_CHARS and len(titulo) > _MAX_TITULO_CHARS:
            titulo = titulo[: _MAX_TITULO_CHARS - 3].rstrip() + "..."
        resumen = _limpiar_texto_noticia(noticia.get("resumen") or "")
        if _MAX_RESUMEN_CHARS and len(resumen) > _MAX_RESUMEN_CHARS:
            resumen = resumen[: _MAX_RESUMEN_CHARS - 3].rstrip() + "..."
        fuente = str(noticia.get("fuente") or "").strip()
        fragmento = f"{indice}. Título: {titulo}."
        if fuente:
            fragmento += f" Fuente: {fuente}."
        if resumen:
            fragmento += f" Resumen: {resumen}"
        longitud_fragmento = len(fragmento) + (1 if partes else 0)
        if _MAX_PROMPT_CHARS and longitud_actual + longitud_fragmento > _MAX_PROMPT_CHARS:
            break
        longitud_actual += longitud_fragmento
        partes.append(fragmento)
    cuerpo = "\n".join(partes)
    return (
        "<|system|>\n" + instruccion + "</s>\n"\
        + "<|user|>\nNoticias:\n" + cuerpo + "\n\nResumen:<|assistant|>"
    )


def _dividir_noticias_en_bloques(noticias: list[Mapping[str, object]]) -> list[list[Mapping[str, object]]]:
    total = len(noticias)
    if total <= 1:
        return [noticias]

    # Buscamos un tamaño de bloque que reduzca el contexto y respete los límites configurados.
    chunk_size = max(1, min(6, math.ceil(total / 4)))
    if _MAX_NOTICIAS_PROMPT:
        chunk_size = min(chunk_size, _MAX_NOTICIAS_PROMPT)
    if chunk_size >= total:
        chunk_size = max(1, total - 1)

    bloques: list[list[Mapping[str, object]]] = []
    for inicio in range(0, total, chunk_size):
        bloques.append(noticias[inicio : inicio + chunk_size])
    return bloques


def _resumir_en_bloques(
    noticias: list[Mapping[str, object]],
    idioma: str,
    modelo: Optional[str],
    nivel_actual: int,
) -> str:
    bloques = _dividir_noticias_en_bloques(noticias)
    if len(bloques) <= 1:
        raise AISummaryError(
            "Los modelos disponibles rechazaron el prompt incluso tras intentar dividir las noticias."
        )

    res_parciales: list[str] = []
    for bloque in bloques:
        parcial = generar_resumen_sentimiento(
            bloque,
            idioma,
            modelo=modelo,
            _permitir_bloques=True,
            _nivel=nivel_actual + 1,
        )
        res_parciales.append(parcial)

    empresa = str(noticias[0].get("empresa")) if noticias else "la compañía analizada"
    noticias_parciales: list[Mapping[str, object]] = [
        {
            "titulo": f"Resumen parcial {indice}",
            "resumen": parcial,
            "fuente": "Síntesis IA",
            "empresa": empresa,
        }
        for indice, parcial in enumerate(res_parciales, start=1)
    ]

    if len(noticias_parciales) == 1:
        return str(noticias_parciales[0].get("resumen") or "").strip()

    return generar_resumen_sentimiento(
        noticias_parciales,
        idioma,
        modelo=modelo,
        _permitir_bloques=True,
        _nivel=nivel_actual + 1,
    )


def _solicitar_resumen(
    modelo: str, headers: Mapping[str, str], payload: Mapping[str, object], prompt: str
) -> tuple[str, str]:
    endpoint = f"{_HF_BASE_URL}/{modelo}"

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
    except requests.RequestException as exc:  # pragma: no cover - dependiente de la red
        raise AISummaryError(f"No se pudo contactar la API de Hugging Face ({exc}).") from exc

    if response.status_code == 401:
        raise AISummaryError("Token de Hugging Face inválido o sin permisos (401).")

    if response.status_code in (403, 404):
        raise _ModelUnavailableError(
            modelo,
            "La API de Hugging Face reportó que el modelo no está disponible para tu token (403/404).",
        )

    if response.status_code == 429:
        raise AISummaryError(
            "La API de Hugging Face devolvió 429 (límite de cuota alcanzado). Intenta más tarde."
        )

    if response.status_code == 400:
        cuerpo = response.text.strip()[:200]
        cuerpo_sanitizado = _sanitize(cuerpo)
        if "index out of range" in cuerpo.lower():
            raise _ModelUnavailableError(
                modelo,
                "El modelo principal rechazó el prompt (index out of range). Intentando con el respaldo.",
            )
        raise AISummaryError(f"La API de Hugging Face devolvió 400: {cuerpo_sanitizado}")

    if response.status_code >= 500:
        raise AISummaryError("La API de Hugging Face está temporalmente indisponible (error 5xx).")

    if response.status_code != 200:
        raise AISummaryError(
            _sanitize(
                f"La API de Hugging Face devolvió un error {response.status_code}: {response.text.strip()[:200]}"
            )
        )

    try:
        data = response.json()
    except ValueError as exc:  # pragma: no cover
        raise AISummaryError("La API de Hugging Face devolvió un cuerpo no válido.") from exc

    if isinstance(data, dict) and data.get("error"):
        # Algunos modelos devuelven errores estructurados en el cuerpo.
        raise AISummaryError(_sanitize(str(data.get("error"))))

    if not isinstance(data, list) or not data:
        raise AISummaryError("La API de Hugging Face no devolvió un resumen válido.")

    primera_respuesta = data[0]
    generated: Optional[str] = None

    if isinstance(primera_respuesta, dict):
        if "generated_text" in primera_respuesta and isinstance(primera_respuesta["generated_text"], str):
            generated = primera_respuesta["generated_text"]
        elif "summary_text" in primera_respuesta and isinstance(primera_respuesta["summary_text"], str):
            generated = primera_respuesta["summary_text"]
    elif isinstance(primera_respuesta, str):
        generated = primera_respuesta

    if not generated or not isinstance(generated, str) or not generated.strip():
        raise AISummaryError("La respuesta de Hugging Face llegó vacía.")

    resumen = generated.replace(prompt, "", 1).strip()
    return (resumen or generated.strip(), modelo)


def _deberia_traducir(model_id: str) -> bool:
    modelo = (model_id or "").lower()
    if not modelo:
        return False
    palabras_clave = ["facebook/bart", "huggingfaceh4/", "zephyr"]
    return any(clave in modelo for clave in palabras_clave)


def _traducir_a_espanol(texto: str, headers: Mapping[str, str]) -> str:
    if not texto or not texto.strip():
        return texto

    modelo_traduccion = os.environ.get("HUGGINGFACE_TRANSLATION_MODEL", _TRANSLATION_MODEL)
    if not modelo_traduccion:
        return texto

    endpoint = f"{_HF_BASE_URL}/{modelo_traduccion}"
    payload = {
        "inputs": texto,
        "parameters": {"clean_up_tokenization_spaces": True},
        "options": {"wait_for_model": True},
    }

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
        response.raise_for_status()
    except requests.RequestException:
        return texto

    try:
        data = response.json()
    except ValueError:
        return texto

    if isinstance(data, list) and data:
        candidate = data[0]
        if isinstance(candidate, dict) and candidate.get("translation_text"):
            return str(candidate["translation_text"]).strip() or texto

    if isinstance(data, dict) and data.get("translation_text"):
        return str(data["translation_text"]).strip() or texto

    return texto


def _asegurar_espanol(texto: str, modelo_utilizado: str, headers: Mapping[str, str]) -> str:
    if not texto:
        return texto

    if os.environ.get("HUGGINGFACE_ALWAYS_TRANSLATE", "false").lower() == "true":
        return _traducir_a_espanol(texto, headers)

    if _deberia_traducir(modelo_utilizado):
        return _traducir_a_espanol(texto, headers)

    return texto


def generar_resumen_sentimiento(
    noticias: Iterable[Mapping[str, object]],
    idioma: str = "es",
    modelo: Optional[str] = None,
    _permitir_bloques: bool = True,
    _nivel: int = 0,
) -> str:
    noticias = list(noticias)
    if not noticias:
        raise AISummaryError("No hay noticias para resumir.")

    api_token = os.environ.get("HUGGINGFACE_API_TOKEN", "").strip()
    if not api_token:
        raise AISummaryError("Definí HUGGINGFACE_API_TOKEN para habilitar el resumen con IA.")

    prompt = _compose_prompt(noticias, idioma)
    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": 320,
            "temperature": 0.3,
            "top_p": 0.9,
            "do_sample": True,
        },
        "options": {
            "wait_for_model": True,
        },
    }

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    modelos_a_probar: list[str] = []
    preferido = modelo or os.environ.get("HUGGINGFACE_SUMMARY_MODEL", _DEFAULT_MODEL)
    if preferido:
        modelos_a_probar.append(preferido)

    fallback_config = os.environ.get("HUGGINGFACE_SUMMARY_FALLBACK", _FALLBACK_MODEL)
    if fallback_config:
        fallback_modelo = fallback_config.strip()
        if fallback_modelo and fallback_modelo not in modelos_a_probar:
            modelos_a_probar.append(fallback_modelo)

    ultimo_error: Optional[AISummaryError] = None
    agotado_por_prompt = False
    for modelo_actual in modelos_a_probar:
        try:
            resumen, utilizado = _solicitar_resumen(modelo_actual, headers, payload, prompt)

            if idioma.lower().startswith("es"):
                resumen = _asegurar_espanol(
                    resumen,
                    utilizado,
                    headers,
                )

            return resumen
        except _ModelUnavailableError as exc:
            ultimo_error = exc
            if "index out of range" in str(exc).lower():
                agotado_por_prompt = True
            # Intentamos con el siguiente modelo disponible.
            continue
        except AISummaryError as exc:
            # Errores diferentes a disponibilidad se devuelven inmediatamente.
            raise
    if ultimo_error:
        if agotado_por_prompt:
            if (
                _permitir_bloques
                and len(noticias) > 1
                and _nivel < 5
            ):
                try:
                    return _resumir_en_bloques(noticias, idioma, modelo, _nivel)
                except AISummaryError:
                    pass
            raise AISummaryError(
                "Los modelos disponibles rechazaron el prompt por exceso de contexto. "
                "Reducimos automáticamente la extensión, pero las noticias siguen siendo demasiado extensas."
            )
        raise ultimo_error

    raise AISummaryError("No se pudo generar el resumen: no hay modelos configurados para intentar.")
