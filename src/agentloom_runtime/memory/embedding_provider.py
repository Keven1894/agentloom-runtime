"""Shared embedding provider for semantic retrieval."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("agentloom-runtime.memory.embedding")

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


@lru_cache(maxsize=1)
def _load_env() -> None:
    env_file = os.environ.get("AGENTLOOM_ENV_FILE")
    env_path = Path(env_file) if env_file else None
    if env_path and env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
        except Exception:
            pass


def get_embedding_model() -> str:
    _load_env()
    return os.environ.get("EMBEDDING_MODEL") or os.environ.get(
        "OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
    )


@lru_cache(maxsize=1)
def _get_openai_client():
    _load_env()
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

    response = client.embeddings.create(model=model or get_embedding_model(), input=clean_texts)
    return [item.embedding for item in response.data]


def embed_query(text: str, model: str | None = None) -> list[float] | None:
    try:
        vectors = embed_texts([text], model=model)
    except Exception as exc:
        logger.warning("[EMBED] Query embedding failed: %s", exc)
        return None
    return vectors[0] if vectors else None
