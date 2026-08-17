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
from agentloom_runtime.session.store import (
    ResumePack,
    SessionRecord,
    add_turn,
    checkpoint,
    close_session,
    list_checkpoints,
    open_session,
    park_session,
    render_resume_pack,
    resume,
    search_sessions,
)
from agentloom_runtime.session.vcs import VcsState, collect_vcs_state

__all__ = [
    "HostContext",
    "ResumePack",
    "SessionRecord",
    "VcsState",
    "add_turn",
    "checkpoint",
    "close_session",
    "collect_vcs_state",
    "detect_host_context",
    "detect_workspace_key",
    "list_checkpoints",
    "normalize_workspace_key",
    "open_session",
    "park_session",
    "render_resume_pack",
    "resolve_operator_id",
    "resume",
    "search_sessions",
]
