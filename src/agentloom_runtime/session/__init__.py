"""Layer 0 working-session memory: cross-host, host-agnostic session continuity.

Where the curated-knowledge, management, and plan/provenance layers answer
"what does the organization know" and "what is the team doing", this layer
answers "where did this agent and this human leave off in this repository" —
and answers it from a shared database rather than from editor-local storage, so
the answer survives switching machines or IDEs.
"""

from agentloom_runtime.session.identity import (
    HostContext,
    detect_host_context,
    detect_workspace_key,
    normalize_workspace_key,
    resolve_operator_id,
)
from agentloom_runtime.session.readers import discover_transcripts, get_reader
from agentloom_runtime.session.store import (
    ArchiveHit,
    ResumePack,
    SessionRecord,
    TranscriptRecord,
    add_turn,
    checkpoint,
    close_session,
    get_session_lineage,
    get_workspace_session_tree,
    index_transcript,
    index_workspace,
    list_checkpoints,
    list_transcripts,
    load_transcript,
    open_session,
    park_session,
    render_resume_pack,
    resume,
    search_archive,
    search_sessions,
    store_transcript,
)
from agentloom_runtime.session.transcript import (
    TranscriptDocument,
    redact,
    render_markdown,
    render_text,
)
from agentloom_runtime.session.vcs import VcsState, collect_vcs_state

__all__ = [
    "ArchiveHit",
    "HostContext",
    "ResumePack",
    "SessionRecord",
    "TranscriptDocument",
    "TranscriptRecord",
    "VcsState",
    "add_turn",
    "checkpoint",
    "close_session",
    "collect_vcs_state",
    "detect_host_context",
    "detect_workspace_key",
    "discover_transcripts",
    "get_reader",
    "get_session_lineage",
    "get_workspace_session_tree",
    "index_transcript",
    "index_workspace",
    "list_checkpoints",
    "list_transcripts",
    "load_transcript",
    "normalize_workspace_key",
    "open_session",
    "park_session",
    "redact",
    "render_markdown",
    "render_resume_pack",
    "render_text",
    "resolve_operator_id",
    "resume",
    "search_archive",
    "search_sessions",
    "store_transcript",
]
