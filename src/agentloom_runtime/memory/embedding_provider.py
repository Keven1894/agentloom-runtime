"""Shared embedding provider for semantic retrieval."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Iterable

from agentloom_runtime.config import load_env

logger = logging.getLogger("agentloom-runtime.memory.embedding")

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# text-embedding-3-small rejects a request over 300k tokens. One 929-turn
# trilingual transcript was 425k in a single call. Stay well under, including
# CJK where chars ≈ tokens.
MAX_EMBED_INPUTS = 128
MAX_EMBED_CHARS = 80_000


def get_embedding_model() -> str:
    load_env()
    return os.environ.get("EMBEDDING_MODEL") or os.environ.get(
        "OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
    )


@lru_cache(maxsize=1)
def _get_openai_client():
    load_env()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    try:
        import openai

        return openai.OpenAI(api_key=api_key)
    except Exception as exc:
        logger.warning("[EMBED] OpenAI unavailable: %s", exc)
        return None


def embed_texts(texts: Iterable[str], model: str | None = None) -> list[list[float]]:
    provider = os.environ.get("EMBEDDING_PROVIDER", "openai").lower()
    if provider != "openai":
        raise RuntimeError(f"Unsupported EMBEDDING_PROVIDER for v1: {provider}")

    clean_texts = [text for text in texts if text and text.strip()]
    if not clean_texts:
        return []

    client = _get_openai_client()
    if client is None:
        raise RuntimeError("OPENAI_API_KEY missing or OpenAI client unavailable")

    model_name = model or get_embedding_model()
    vectors: list[list[float]] = []
    batch: list[str] = []
    chars = 0
    for text in clean_texts:
        n = len(text)
        if batch and (len(batch) >= MAX_EMBED_INPUTS or chars + n > MAX_EMBED_CHARS):
            vectors.extend(_embed_batch(client, model_name, batch))
            batch, chars = [], 0
        batch.append(text)
        chars += n
    if batch:
        vectors.extend(_embed_batch(client, model_name, batch))
    return vectors


def _embed_batch(client: Any, model: str, batch: list[str]) -> list[list[float]]:
    response = client.embeddings.create(model=model, input=batch)
    return [item.embedding for item in response.data]


def embed_query(text: str, model: str | None = None) -> list[float] | None:
    try:
        vectors = embed_texts([text], model=model)
    except Exception as exc:
        logger.warning("[EMBED] Query embedding failed: %s", exc)
        return None
    return vectors[0] if vectors else None
