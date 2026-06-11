"""In-process DocShare embedding index for fast semantic search."""

from __future__ import annotations

import json
import logging
import threading

import numpy as np

from agentloom_runtime.db import connect
from agentloom_runtime.memory.embedding_provider import get_embedding_model
from agentloom_runtime.memory.sync import embeddings_sync_mtime

logger = logging.getLogger("agentloom-runtime.memory.docshare_index")

_lock = threading.Lock()
_matrix: np.ndarray | None = None
_doc_ids: list[str] | None = None
_model: str | None = None
_sync_mtime: float | None = None
_row_count: int = 0


def invalidate_docshare_embedding_index() -> None:
    """Drop the in-memory matrix (call after embedding rebuild)."""
    global _matrix, _doc_ids, _model, _sync_mtime, _row_count
    with _lock:
        _matrix = None
        _doc_ids = None
        _model = None
        _sync_mtime = None
        _row_count = 0
    logger.info("[DocShare] embedding index invalidated")


def _load_matrix(model: str) -> tuple[np.ndarray, list[str]]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT e.doc_id, e.embedding
            FROM docshare_embeddings e
            WHERE e.embedding_model = ? AND e.embedding IS NOT NULL
            """,
            (model,),
        ).fetchall()
    finally:
        conn.close()

    doc_ids: list[str] = []
    vectors: list[list[float]] = []
    for row in rows:
        try:
            embedding = row["embedding"]
            if isinstance(embedding, str):
                embedding = json.loads(embedding)
            if not embedding:
                continue
            doc_ids.append(row["doc_id"])
            vectors.append(embedding)
        except Exception:
            continue

    if not vectors:
        return np.zeros((0, 0), dtype=np.float32), []

    matrix = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= norms + 1e-9
    return matrix, doc_ids


def _ensure_matrix(model: str | None = None) -> tuple[np.ndarray, list[str]]:
    global _matrix, _doc_ids, _model, _sync_mtime, _row_count
    model = model or get_embedding_model()
    mtime = embeddings_sync_mtime("docshare")
    with _lock:
        if _matrix is not None and _model == model and _sync_mtime == mtime:
            return _matrix, _doc_ids or []

        matrix, doc_ids = _load_matrix(model)
        _matrix = matrix
        _doc_ids = doc_ids
        _model = model
        _sync_mtime = mtime
        _row_count = len(doc_ids)
        logger.info("[DocShare] embedding index loaded: %s rows, model=%s", _row_count, model)
        return _matrix, _doc_ids


def search_doc_ids_by_vector(
    query_vec: list[float],
    *,
    limit: int = 25,
    min_score: float = 0.25,
    model: str | None = None,
) -> list[tuple[str, float]]:
    """Return (doc_id, cosine score) sorted by score descending."""
    matrix, doc_ids = _ensure_matrix(model)
    if matrix.size == 0 or not doc_ids:
        return []

    q = np.array(query_vec, dtype=np.float32)
    q /= np.linalg.norm(q) + 1e-9
    scores = matrix @ q
    if limit >= len(scores):
        top_idx = np.argsort(scores)[::-1]
    else:
        top_idx = np.argpartition(scores, -limit)[-limit:]
        top_idx = top_idx[np.argsort(scores[top_idx])[::-1]]

    results: list[tuple[str, float]] = []
    for idx in top_idx:
        score = float(scores[idx])
        if score < min_score:
            continue
        results.append((doc_ids[int(idx)], score))
    return results[:limit]
