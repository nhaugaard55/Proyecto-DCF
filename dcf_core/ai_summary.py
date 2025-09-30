"""Generates market sentiment summaries using Hugging Face Inference API."""

from __future__ import annotations

import os
import re
from typing import Iterable, Mapping, Optional

import requests


class AISummaryError(RuntimeError):
    """Raised when the sentiment summary could not be generated."""


_DEFAULT_MODEL = "mistralai/Mistral-7B-Instruct-v0.2"
_HF_BASE_URL = "https://api-inference.huggingface.co/models"


def _sanitize(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"apikey=[^&\s]+", "apikey=****", text, flags=re.IGNORECASE)
    text = re.sub(r"token=[^&\s]+", "token=****", text, flags=re.IGNORECASE)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ****", text, flags=re.IGNORECASE)
    return text


def _compose_prompt(noticias: Iterable[Mapping[str, object]], idioma: str) -> str:
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
        f"Eres un analista financiero. Leyendo los titulares listados, redacta un breve resumen en {idioma} "
        "sobre el sentimiento actual del mercado respecto a la empresa (positivo, negativo o mixto) "
        "e incluye los temas principales. Sé muy conciso (máximo tres frases)."
    )
    return (
        f"<s>[INST] {instruccion}\n\nNoticias:\n{cuerpo}\n\nResumen: [/INST]"
    )


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

    modelo = modelo or os.environ.get("HUGGINGFACE_SUMMARY_MODEL", _DEFAULT_MODEL)
    endpoint = f"{_HF_BASE_URL}/{modelo}"

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

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
    except requests.RequestException as exc:  # pragma: no cover - dependiente de la red
        raise AISummaryError(f"No se pudo contactar la API de Hugging Face ({exc}).") from exc

    if response.status_code == 429:
        raise AISummaryError("La API de Hugging Face devolvió 429 (límite de cuota alcanzado). Intenta más tarde.")

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
        raise AISummaryError(_sanitize(str(data.get("error"))))

    if not isinstance(data, list) or not data:
        raise AISummaryError("La API de Hugging Face no devolvió un resumen válido.")

    generated = data[0].get("generated_text") if isinstance(data[0], dict) else None
    if not isinstance(generated, str) or not generated.strip():
        raise AISummaryError("La respuesta de Hugging Face llegó vacía.")

    resumen = generated.replace(prompt, "", 1).strip()
    return resumen or generated.strip()
