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


_QUERY_MEMORY: dict[str, list[float]] = {}


def _query_cache_key(model: str, text: str) -> str:
    import hashlib
    import re

    normalized = re.sub(r"\s+", " ", (text or "").strip())
    return hashlib.sha256(f"{model}\n0\n{normalized}".encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _query_cache_table() -> bool:
    try:
        from agentloom_runtime.db import connect

        conn = connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'query_embedding_cache'"
            ).fetchone()
            return bool(row and int(row["n"]))
        finally:
            conn.close()
    except Exception:
        return False


def _query_cache_read(key: str) -> list[float] | None:
    if not _query_cache_table():
        return None
    try:
        import json

        from agentloom_runtime.db import connect

        conn = connect()
        try:
            row = conn.execute(
                "SELECT embedding FROM query_embedding_cache WHERE cache_key = ?", [key]
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        value = row["embedding"]
        if isinstance(value, (bytes, str)):
            value = json.loads(value)
        return [float(x) for x in value] if value else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EMBED] query cache read failed: %s", exc)
        return None


def _query_cache_write(key: str, model: str, text: str, vector: list[float]) -> None:
    if not _query_cache_table():
        return
    try:
        import json
        import re

        from agentloom_runtime.db import connect

        conn = connect()
        try:
            conn.execute(
                "INSERT IGNORE INTO query_embedding_cache "
                "(cache_key, embedding_model, dimensions, query_norm, embedding) "
                "VALUES (?, ?, ?, ?, ?)",
                [key, model, len(vector), re.sub(r"\s+", " ", text.strip())[:512], json.dumps(vector)],
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EMBED] query cache write failed: %s", exc)


def embed_query(text: str, model: str | None = None) -> list[float] | None:
    """Embed one query. Repeats are served from the query_embedding_cache table."""
    model_name = model or get_embedding_model()
    key = _query_cache_key(model_name, text)
    hit = _QUERY_MEMORY.get(key) or _query_cache_read(key)
    if hit:
        _QUERY_MEMORY[key] = hit
        return hit
    try:
        vectors = embed_texts([text], model=model_name)
    except Exception as exc:
        logger.warning("[EMBED] Query embedding failed: %s", exc)
        return None
    vector = vectors[0] if vectors else None
    if vector:
        _QUERY_MEMORY[key] = vector
        _query_cache_write(key, model_name, text, vector)
    return vector
