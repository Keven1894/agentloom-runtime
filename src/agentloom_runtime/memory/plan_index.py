"""In-process plan_embeddings matrix cache for fast plan memory search."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import numpy as np

from agentloom_runtime.db import connect
from agentloom_runtime.memory.embedding_provider import get_embedding_model
from agentloom_runtime.memory.sync import embeddings_sync_mtime

logger = logging.getLogger("agentloom-runtime.memory.plan_index")

_lock = threading.Lock()
_matrix: np.ndarray | None = None
_meta: list[dict[str, Any]] | None = None
_model: str | None = None
_sync_mtime: float | None = None
_row_count: int = 0


def invalidate_plan_embedding_index() -> None:
    global _matrix, _meta, _model, _sync_mtime, _row_count
    with _lock:
        _matrix = None
        _meta = None
        _model = None
        _sync_mtime = None
        _row_count = 0
    logger.info("[Plan] embedding index invalidated")


def _load_matrix(model: str) -> tuple[np.ndarray, list[dict[str, Any]]]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, path, title, lifecycle, status, owner, project_id, task_id,
                   message_id, tags, content, embedding
            FROM plan_embeddings
            WHERE embedding IS NOT NULL AND embedding_model = ?
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
            tags = row["tags"]
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            meta.append(
                {
                    "id": row["id"],
                    "path": row["path"],
                    "title": row["title"],
                    "lifecycle": row["lifecycle"],
                    "status": row["status"],
                    "owner": row["owner"],
                    "project_id": row["project_id"],
                    "task_id": row["task_id"],
                    "message_id": row["message_id"],
                    "tags": tags,
                    "content": row["content"],
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


def _matches_filters(row: dict[str, Any], filters: dict[str, str | None]) -> bool:
    for key in ("lifecycle", "status", "owner", "project_id", "task_id"):
        value = filters.get(key)
        if value and str(row.get(key) or "") != str(value):
            return False
    return True


def _ensure_matrix(model: str | None = None) -> tuple[np.ndarray, list[dict[str, Any]]]:
    global _matrix, _meta, _model, _sync_mtime, _row_count
    model = model or get_embedding_model()
    mtime = embeddings_sync_mtime("plan")
    with _lock:
        if _matrix is not None and _model == model and _sync_mtime == mtime:
            return _matrix, _meta or []

        matrix, meta = _load_matrix(model)
        _matrix = matrix
        _meta = meta
        _model = model
        _sync_mtime = mtime
        _row_count = len(meta)
        logger.info("[Plan] embedding index loaded: %s rows, model=%s", _row_count, model)
        return _matrix, _meta


def search_plan_chunks_by_vector(
    query_vec: list[float],
    *,
    limit: int = 25,
    min_score: float = 0.0,
    filters: dict[str, str | None] | None = None,
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
    for idx in top_idx:
        score = float(scores[idx])
        if score < min_score:
            break
        row = meta[int(idx)]
        if not _matches_filters(row, filters):
            continue
        results.append((row, score))
        if len(results) >= limit:
            break
    return results
