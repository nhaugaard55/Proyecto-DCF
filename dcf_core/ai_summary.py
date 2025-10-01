"""Generates market sentiment summaries using Hugging Face Inference API."""

from __future__ import annotations

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
_HF_BASE_URL = "https://api-inference.huggingface.co/models"


def _sanitize(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"apikey=[^&\s]+", "apikey=****", text, flags=re.IGNORECASE)
    text = re.sub(r"token=[^&\s]+", "token=****", text, flags=re.IGNORECASE)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ****", text, flags=re.IGNORECASE)
    return text


def _compose_prompt(noticias: Iterable[Mapping[str, object]], idioma: str) -> str:
    noticias = list(noticias)
    empresa = str(noticias[0].get("empresa")) if noticias else "la compañía analizada"
    partes: list[str] = []
    for indice, noticia in enumerate(noticias, start=1):
        titulo = str(noticia.get("titulo") or "").strip()
        resumen = str(noticia.get("resumen") or "").strip()
        fuente = str(noticia.get("fuente") or "").strip()
        fragmento = f"{indice}. Título: {titulo}."
        if fuente:
            fragmento += f" Fuente: {fuente}."
        if resumen:
            fragmento += f" Resumen: {resumen}"
        partes.append(fragmento)
    cuerpo = "\n".join(partes)
    instruccion = (
        f"Eres un analista financiero. Analiza únicamente lo que indican las noticias sobre {empresa}. "
        f"Redacta un breve resumen en {idioma} explicando si el sentimiento hacia {empresa} es positivo, "
        "negativo o mixto y qué temas concretos afectan a la compañía. Ignora menciones a otras empresas "
        "o al mercado general. Sé muy conciso (máximo tres frases) y usa lenguaje neutral."
    )
    return (
        "<|system|>\n" + instruccion + "</s>\n"\
        + "<|user|>\nNoticias:\n" + cuerpo + "\n\nResumen:<|assistant|>"
    )


def _solicitar_resumen(modelo: str, headers: Mapping[str, str], payload: Mapping[str, object], prompt: str) -> str:
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
    return resumen or generated.strip()


def generar_resumen_sentimiento(
    noticias: Iterable[Mapping[str, object]],
    idioma: str = "es",
    modelo: Optional[str] = None,
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
            "max_new_tokens": 160,
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
    for modelo_actual in modelos_a_probar:
        try:
            return _solicitar_resumen(modelo_actual, headers, payload, prompt)
        except _ModelUnavailableError as exc:
            ultimo_error = exc
            # Intentamos con el siguiente modelo disponible.
            continue
        except AISummaryError as exc:
            # Errores diferentes a disponibilidad se devuelven inmediatamente.
            raise

    if ultimo_error:
        raise ultimo_error

    raise AISummaryError("No se pudo generar el resumen: no hay modelos configurados para intentar.")
