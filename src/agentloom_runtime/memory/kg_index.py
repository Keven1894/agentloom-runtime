"""In-process knowledge_embeddings matrix cache for fast KG semantic search."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import numpy as np

from agentloom_runtime.db import connect
from agentloom_runtime.memory.sync import embeddings_sync_mtime

logger = logging.getLogger("agentloom-runtime.memory.kg_index")

_lock = threading.Lock()
_matrix: np.ndarray | None = None
_row_ids: list[str] | None = None
_row_meta: list[dict[str, Any]] | None = None
_sync_mtime: float | None = None
_row_count: int = 0


def invalidate_kg_embedding_index() -> None:
    global _matrix, _row_ids, _row_meta, _sync_mtime, _row_count
    with _lock:
        _matrix = None
        _row_ids = None
        _row_meta = None
        _sync_mtime = None
        _row_count = 0
    logger.info("[KG] embedding index invalidated")


def _load_matrix() -> tuple[np.ndarray, list[str], list[dict[str, Any]]]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, node_id, node_type, source_file, topic, content, embedding
            FROM knowledge_embeddings
            WHERE embedding IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()

    row_ids: list[str] = []
    meta: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    for row in rows:
        try:
            embedding = row["embedding"]
            if isinstance(embedding, str):
                embedding = json.loads(embedding)
            if not embedding:
                continue
            row_ids.append(row["id"])
            meta.append(
                {
                    "id": row["id"],
                    "node_id": row["node_id"],
                    "node_type": row["node_type"],
                    "source_file": row["source_file"],
                    "topic": row["topic"],
                    "content": row["content"],
                }
            )
            vectors.append(embedding)
        except Exception:
            continue

    if not vectors:
        return np.zeros((0, 0), dtype=np.float32), [], []

    matrix = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= norms + 1e-9
    return matrix, row_ids, meta


def _ensure_matrix() -> tuple[np.ndarray, list[str], list[dict[str, Any]]]:
    global _matrix, _row_ids, _row_meta, _sync_mtime, _row_count
    mtime = embeddings_sync_mtime("kg")
    with _lock:
        if _matrix is not None and _sync_mtime == mtime:
            return _matrix, _row_ids or [], _row_meta or []

        matrix, row_ids, meta = _load_matrix()
        _matrix = matrix
        _row_ids = row_ids
        _row_meta = meta
        _sync_mtime = mtime
        _row_count = len(row_ids)
        logger.info("[KG] embedding index loaded: %s rows", _row_count)
        return _matrix, _row_ids, _row_meta


def search_kg_ids_by_vector(
    query_vec: list[float],
    *,
    limit: int = 25,
    min_score: float = 0.0,
    node_types: list[str] | None = None,
) -> list[tuple[str, float, dict[str, Any]]]:
    """Return (row_id, cosine score, meta) sorted by score descending."""
    matrix, _row_ids, meta = _ensure_matrix()
    if matrix.size == 0 or not meta:
        return []

    q = np.array(query_vec, dtype=np.float32)
    q /= np.linalg.norm(q) + 1e-9
    scores = matrix @ q

    type_set = {t.lower() for t in node_types} if node_types else None
    ranked: list[tuple[int, float]] = []
    for idx, score in enumerate(scores):
        if type_set and (meta[idx].get("node_type") or "").lower() not in type_set:
            continue
        ranked.append((idx, float(score)))

    ranked.sort(key=lambda item: item[1], reverse=True)
    results: list[tuple[str, float, dict[str, Any]]] = []
    for idx, score in ranked:
        if score < min_score:
            continue
        row_id = meta[idx]["id"]
        results.append((row_id, score, meta[idx]))
        if len(results) >= limit:
            break
    return results
