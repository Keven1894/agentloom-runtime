"""RRF joint ranking over knowledge_embeddings and docshare_embeddings."""

from __future__ import annotations

import logging
import os
from typing import Any

from agentloom_runtime.memory.docshare_index import search_doc_ids_by_vector
from agentloom_runtime.memory.embedding_provider import embed_query, get_embedding_model
from agentloom_runtime.memory.kg_index import search_kg_ids_by_vector

logger = logging.getLogger("agentloom-runtime.memory.joint_retrieval")

DEFAULT_RRF_K = 60


def retrieval_use_rrf() -> bool:
    return os.environ.get("AGENTLOOM_RETRIEVAL_USE_RRF", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[str, dict[str, Any]]]],
    *,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[tuple[str, float, dict[str, Any]]]:
    """Fuse multiple ranked lists. Each item is (key, payload)."""
    scores: dict[str, float] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for ranking in ranked_lists:
        for rank, (key, payload) in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank)
            payloads.setdefault(key, payload)
    fused = [(key, scores[key], payloads[key]) for key in scores]
    fused.sort(key=lambda item: item[1], reverse=True)
    return fused


def search_kg_docshare_joint(
    query: str,
    *,
    top_k: int = 8,
    top_k_per_store: int = 12,
    min_score: float = 0.22,
    node_types: list[str] | None = None,
    use_rrf: bool | None = None,
    rrf_k: int = DEFAULT_RRF_K,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """
    Search KG + DocShare with optional RRF fusion.

    Returns (kg_hits, docshare_hits, ranking_mode) where each hit includes
    ``score``, ``search_mode``, and store-specific fields.
    """
    query = (query or "").strip()
    if not query:
        return [], [], "empty"

    use_rrf = retrieval_use_rrf() if use_rrf is None else use_rrf
    model = get_embedding_model()
    query_vec = embed_query(query, model=model)
    if not query_vec:
        return [], [], "no_embed"

    kg_ranked = search_kg_ids_by_vector(
        query_vec,
        limit=top_k_per_store,
        min_score=0.0,
        node_types=node_types,
    )
    doc_ranked = search_doc_ids_by_vector(
        query_vec,
        limit=top_k_per_store,
        min_score=0.0,
        model=model,
    )

    if not use_rrf:
        kg_hits = [
            {
                "id": meta["id"],
                "node_id": meta.get("node_id"),
                "node_type": meta.get("node_type"),
                "source_file": meta.get("source_file"),
                "topic": meta.get("topic"),
                "content": meta.get("content"),
                "score": score,
                "search_mode": "vector-index",
            }
            for _row_id, score, meta in kg_ranked
            if score >= min_score
        ][:top_k]
        doc_hits = [
            {
                "doc_id": doc_id,
                "score": score,
                "search_mode": "vector-index",
            }
            for doc_id, score in doc_ranked
            if score >= min_score
        ][:top_k]
        return kg_hits, doc_hits, "cosine"

    kg_list = [
        (
            f"kg:{meta.get('node_id') or row_id}",
            {
                "store": "kg",
                "id": meta["id"],
                "node_id": meta.get("node_id"),
                "node_type": meta.get("node_type"),
                "source_file": meta.get("source_file"),
                "topic": meta.get("topic"),
                "content": meta.get("content"),
                "cosine_score": score,
            },
        )
        for row_id, score, meta in kg_ranked
    ]
    doc_list = [
        (
            f"doc:{doc_id}",
            {
                "store": "docshare",
                "doc_id": doc_id,
                "cosine_score": score,
            },
        )
        for doc_id, score in doc_ranked
    ]
    fused = reciprocal_rank_fusion([kg_list, doc_list], rrf_k=rrf_k)

    kg_hits: list[dict[str, Any]] = []
    doc_hits: list[dict[str, Any]] = []
    for _key, rrf_score, payload in fused:
        if payload.get("store") == "kg":
            kg_hits.append(
                {
                    "id": payload["id"],
                    "node_id": payload.get("node_id"),
                    "node_type": payload.get("node_type"),
                    "source_file": payload.get("source_file"),
                    "topic": payload.get("topic"),
                    "content": payload.get("content"),
                    "score": rrf_score,
                    "cosine_score": payload.get("cosine_score"),
                    "search_mode": "rrf",
                }
            )
        else:
            doc_hits.append(
                {
                    "doc_id": payload["doc_id"],
                    "score": rrf_score,
                    "cosine_score": payload.get("cosine_score"),
                    "search_mode": "rrf",
                }
            )
        if len(kg_hits) + len(doc_hits) >= top_k * 2:
            break

    logger.info(
        "[Joint] RRF '%s' → kg=%s docshare=%s (k=%s)",
        query[:50],
        len(kg_hits),
        len(doc_hits),
        rrf_k,
    )
    return kg_hits[:top_k], doc_hits[:top_k], "rrf"
