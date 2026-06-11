"""In-process message_embeddings matrix cache for fast semantic search."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import numpy as np

from agentloom_runtime.db import connect
from agentloom_runtime.memory.embedding_provider import get_embedding_model
from agentloom_runtime.memory.sync import embeddings_sync_mtime

logger = logging.getLogger("agentloom-runtime.memory.message_index")

_lock = threading.Lock()
_matrix: np.ndarray | None = None
_meta: list[dict[str, Any]] | None = None
_model: str | None = None
_sync_mtime: float | None = None
_row_count: int = 0


def invalidate_message_embedding_index() -> None:
    global _matrix, _meta, _model, _sync_mtime, _row_count
    with _lock:
        _matrix = None
        _meta = None
        _model = None
        _sync_mtime = None
        _row_count = 0
    logger.info("[Message] embedding index invalidated")


def _load_matrix(model: str) -> tuple[np.ndarray, list[dict[str, Any]]]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT me.id AS chunk_id, me.message_id, me.reply_id, me.chunk_type,
                   me.content, me.embedding,
                   m.title, m.reporter, m.recipient, m.category, m.severity,
                   m.status, m.project_id, m.task_id, m.created_at
            FROM message_embeddings me
            JOIN messages m ON m.id = me.message_id
            WHERE me.embedding IS NOT NULL AND me.embedding_model = ?
            """,
            (model,),
        ).fetchall()
    finally:
        conn.close()

    meta: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    for row in rows:
        try:
            embedding = row["embedding"]
            if isinstance(embedding, str):
                embedding = json.loads(embedding)
            if not embedding:
                continue
            meta.append(
                {
                    "chunk_id": row["chunk_id"],
                    "message_id": row["message_id"],
                    "reply_id": row["reply_id"],
                    "chunk_type": row["chunk_type"],
                    "content": row["content"],
                    "title": row["title"],
                    "reporter": row["reporter"],
                    "recipient": row["recipient"],
                    "category": row["category"],
                    "severity": row["severity"],
                    "status": row["status"],
                    "project_id": row["project_id"],
                    "task_id": row["task_id"],
                    "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
                }
            )
            vectors.append(embedding)
        except Exception:
            continue

    if not vectors:
        return np.zeros((0, 0), dtype=np.float32), []

    matrix = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= norms + 1e-9
    return matrix, meta


def _matches_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    agent_name = filters.get("agent_name")
    if agent_name:
        agent = str(agent_name).lower()
        if agent not in {
            str(row.get("reporter") or "").lower(),
            str(row.get("recipient") or "").lower(),
        }:
            return False
    for key in ("project_id", "task_id", "category", "status"):
        value = filters.get(key)
        if value and str(row.get(key) or "") != str(value):
            return False
    return True


def _ensure_matrix(model: str | None = None) -> tuple[np.ndarray, list[dict[str, Any]]]:
    global _matrix, _meta, _model, _sync_mtime, _row_count
    model = model or get_embedding_model()
    mtime = embeddings_sync_mtime("message")
    with _lock:
        if _matrix is not None and _model == model and _sync_mtime == mtime:
            return _matrix, _meta or []

        matrix, meta = _load_matrix(model)
        _matrix = matrix
        _meta = meta
        _model = model
        _sync_mtime = mtime
        _row_count = len(meta)
        logger.info("[Message] embedding index loaded: %s rows, model=%s", _row_count, model)
        return _matrix, _meta


def search_message_chunks_by_vector(
    query_vec: list[float],
    *,
    limit: int = 25,
    min_score: float = 0.0,
    filters: dict[str, Any] | None = None,
    model: str | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """Return (meta, cosine score) sorted by score descending."""
    matrix, meta = _ensure_matrix(model)
    if matrix.size == 0 or not meta:
        return []

    filters = filters or {}
    q = np.array(query_vec, dtype=np.float32)
    q /= np.linalg.norm(q) + 1e-9
    scores = matrix @ q

    top_idx = np.argsort(scores)[::-1]
    results: list[tuple[dict[str, Any], float]] = []
    seen_chunks: set[tuple[Any, Any, Any]] = set()
    for idx in top_idx:
        score = float(scores[idx])
        if score < min_score:
            break
        row = meta[int(idx)]
        if not _matches_filters(row, filters):
            continue
        dedupe_key = (row["message_id"], row["chunk_type"], row["reply_id"])
        if dedupe_key in seen_chunks:
            continue
        seen_chunks.add(dedupe_key)
        results.append((row, score))
        if len(results) >= limit:
            break
    return results
