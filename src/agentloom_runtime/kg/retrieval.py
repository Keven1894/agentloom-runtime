"""Semantic search over the knowledge_embeddings table."""

from __future__ import annotations

import logging
from typing import Any

from agentloom_runtime.db import connect
from agentloom_runtime.memory.embedding_provider import embed_query, get_embedding_model
from agentloom_runtime.memory.kg_index import search_kg_ids_by_vector

logger = logging.getLogger("agentloom-runtime.kg.retrieval")


def _keyword_search(
    query: str,
    top_k: int,
    node_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Simple SQL LIKE search when embeddings are unavailable."""
    conn = connect()
    words = [w.strip() for w in query.lower().split() if len(w.strip()) > 3]
    if not words:
        conn.close()
        return []

    conditions = " OR ".join(
        "(LOWER(content) LIKE ? OR LOWER(topic) LIKE ?)" for _ in words
    )
    params: list[Any] = []
    for word in words:
        params.extend([f"%{word}%", f"%{word}%"])

    type_filter = ""
    if node_types:
        placeholders = ",".join("?" * len(node_types))
        type_filter = f" AND node_type IN ({placeholders})"
        params.extend(node_types)

    sql = f"""
        SELECT id, source_file, node_id, node_type, topic, content
        FROM knowledge_embeddings
        WHERE ({conditions}){type_filter}
        ORDER BY length(content) DESC
        LIMIT ?
    """
    params.append(top_k)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return [
        {
            "id": row["id"],
            "source_file": row["source_file"],
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "topic": row["topic"],
            "content": row["content"],
            "score": 0.5,
            "search_mode": "keyword",
        }
        for row in rows
    ]


def _vector_search(
    query_vec: list[float],
    top_k: int,
    node_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    ranked = search_kg_ids_by_vector(
        query_vec,
        limit=top_k,
        min_score=0.0,
        node_types=node_types,
    )
    return [
        {
            "id": meta["id"],
            "source_file": meta.get("source_file"),
            "node_id": meta.get("node_id"),
            "node_type": meta.get("node_type"),
            "topic": meta.get("topic"),
            "content": meta.get("content"),
            "score": score,
            "search_mode": "vector-index",
        }
        for _row_id, score, meta in ranked
    ]


def search_kg(
    query: str,
    top_k: int = 5,
    node_types: list[str] | None = None,
    min_score: float = 0.3,
) -> list[dict[str, Any]]:
    """
    Search the knowledge graph for content relevant to the query.

    Returns list of dicts sorted by descending score. Uses vector search when
    embeddings are available, otherwise keyword fallback.
    """
    if not query or not query.strip():
        return []

    model = get_embedding_model()
    query_vec = embed_query(query.strip(), model=model)
    if query_vec:
        results = _vector_search(query_vec, top_k, node_types)
        results = [row for row in results if row["score"] >= min_score]
        if results:
            logger.info(
                "[KG] Vector search '%s' → %s results (top score: %.3f)",
                query[:60],
                len(results),
                results[0]["score"],
            )
            return results
        logger.info(
            "[KG] Vector search returned no results above min_score=%s, falling back to keyword",
            min_score,
        )

    results = _keyword_search(query, top_k, node_types)
    logger.info("[KG] Keyword search '%s' → %s results", query[:60], len(results))
    return results


def format_for_prompt(results: list[dict[str, Any]], max_chars: int = 2000) -> str:
    """Format KG search results as a compact prompt section."""
    if not results:
        return "(no relevant knowledge found)"

    parts: list[str] = []
    total = 0
    for row in results:
        node_type = (row.get("node_type") or "unknown").upper()
        header = f"[{node_type}] {row['topic']} (score: {row['score']:.2f})"
        body = row["content"][:600]
        block = f"{header}\n{body}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)

    return "\n\n---\n\n".join(parts)
