"""In-process embedding indexes and joint retrieval for AgentLoom runtime."""

from agentloom_runtime.memory.docshare_index import (
    invalidate_docshare_embedding_index,
    search_doc_ids_by_vector,
)
from agentloom_runtime.memory.embedding_provider import (
    embed_query,
    embed_texts,
    get_embedding_model,
)
from agentloom_runtime.memory.joint_retrieval import (
    reciprocal_rank_fusion,
    retrieval_use_rrf,
    search_kg_docshare_joint,
)
from agentloom_runtime.memory.kg_index import (
    invalidate_kg_embedding_index,
    search_kg_ids_by_vector,
)
from agentloom_runtime.memory.message_index import (
    invalidate_message_embedding_index,
    search_message_chunks_by_vector,
)
from agentloom_runtime.memory.plan_index import (
    invalidate_plan_embedding_index,
    search_plan_chunks_by_vector,
)
from agentloom_runtime.memory.sync import (
    clear_embedding_sync_markers,
    embeddings_sync_mtime,
    embeddings_sync_timestamp,
    register_embedding_sync_marker,
)

__all__ = [
    "clear_embedding_sync_markers",
    "embed_query",
    "embed_texts",
    "embeddings_sync_mtime",
    "embeddings_sync_timestamp",
    "get_embedding_model",
    "invalidate_docshare_embedding_index",
    "invalidate_kg_embedding_index",
    "invalidate_message_embedding_index",
    "invalidate_plan_embedding_index",
    "reciprocal_rank_fusion",
    "register_embedding_sync_marker",
    "retrieval_use_rrf",
    "search_doc_ids_by_vector",
    "search_kg_docshare_joint",
    "search_kg_ids_by_vector",
    "search_message_chunks_by_vector",
    "search_plan_chunks_by_vector",
]
