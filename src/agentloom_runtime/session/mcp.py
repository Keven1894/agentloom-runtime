"""Model Context Protocol (MCP) server for AgentLoom Layer 0 Session Memory.

Exposes session search, context retrieval, checkpoint inspection, and lineage
tools over standard stdio JSON-RPC. Compatible with Cursor, VSCode/Cline,
Claude Desktop, OpenCode, and any MCP-compliant coding host.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from agentloom_runtime.session import store
from agentloom_runtime.session.identity import (
    detect_host_context,
    detect_workspace_key,
    resolve_lane,
    resolve_operator_id,
)
from agentloom_runtime.session.transcript import render_markdown, render_text

SERVER_NAME = "agentloom-session"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "session_search",
        "description": (
            "Search archived conversations in AgentLoom Layer 0 session memory using "
            "hybrid lexical and vector search. Returns matching turn snippets and pointers "
            "(source_ref, seq) that can be inspected with session_get_context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language or keyword query describing what was discussed, decided, or planned.",
                },
                "workspace_key": {
                    "type": "string",
                    "description": "Normalized VCS remote workspace key (e.g. 'github.com/org/repo'). Defaults to current repository.",
                },
                "since": {
                    "type": "string",
                    "description": "Optional ISO timestamp filter (e.g. '2026-08-01') to restrict search to recent conversations.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of search hits to return (default: 8).",
                },
                "lexical_only": {
                    "type": "boolean",
                    "description": "Force lexical search without vector embeddings (default: false).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "session_get_context",
        "description": (
            "Retrieve sanitized conversation turns around a specific sequence number from "
            "an archived transcript. Use this after session_search to read the full context "
            "of a historical decision or exchange."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "around_seq": {
                    "type": "integer",
                    "description": "Centre sequence number from a search hit.",
                },
                "source_ref": {
                    "type": "string",
                    "description": "Transcript file reference / UUID (e.g. '1b48ad5b-9c93-457f-b475-51b9a7f34e39').",
                },
                "transcript_id": {
                    "type": "string",
                    "description": "Explicit database transcript UUID.",
                },
                "radius": {
                    "type": "integer",
                    "description": "Number of turns before and after around_seq to include (default: 8).",
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "text"],
                    "description": "Rendering format (default: 'markdown').",
                },
            },
            "required": ["around_seq"],
        },
    },
    {
        "name": "session_get_checkpoint",
        "description": (
            "Retrieve the latest checkpoint (or specific session checkpoints) including "
            "next actions, open plan, and key decisions for a session or current workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Explicit session UUID to inspect.",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent identifier (defaults to AGENTLOOM_AGENT_ID).",
                },
                "operator_id": {
                    "type": "string",
                    "description": "Operator identifier (defaults to AGENTLOOM_OPERATOR_ID or the OS user).",
                },
                "workspace_key": {
                    "type": "string",
                    "description": "Workspace key (defaults to current repository).",
                },
                "lane": {
                    "type": "string",
                    "description": (
                        "Concurrent work stream within the workspace (defaults to "
                        "AGENTLOOM_SESSION_LANE or 'default'). Machines working "
                        "different streams at the same time use different lanes."
                    ),
                },
            },
        },
    },
    {
        "name": "session_get_lineage",
        "description": (
            "Retrieve the session ancestry DAG (parent session chain, child branches, "
            "and fork reasons) to understand how the current task relates to work done "
            "on other machines or previous sessions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": (
                        "Session UUID to inspect. Omit to use the calling agent's own "
                        "session in the current workspace."
                    ),
                },
                "agent_id": {
                    "type": "string",
                    "description": "Agent identifier (defaults to AGENTLOOM_AGENT_ID).",
                },
                "operator_id": {
                    "type": "string",
                    "description": "Operator identifier (defaults to AGENTLOOM_OPERATOR_ID or the OS user).",
                },
                "workspace_key": {
                    "type": "string",
                    "description": "Workspace key (defaults to current repository).",
                },
                "lane": {
                    "type": "string",
                    "description": (
                        "Concurrent work stream within the workspace (defaults to "
                        "AGENTLOOM_SESSION_LANE or 'default')."
                    ),
                },
            },
        },
    },
]


def _resolve_workspace(override: Optional[str]) -> str:
    if override:
        return override
    try:
        return detect_workspace_key()
    except Exception:
        return ""


def _resolve_identity(arguments: dict[str, Any]) -> tuple[str, str, str]:
    """Resolve the (agent, operator, workspace) triple a tool call acts on.

    The runtime ships no default agent name. A deployment names its own agents,
    so an unset ``AGENTLOOM_AGENT_ID`` is a configuration error to report, not a
    value to invent — guessing would silently read another agent's session.
    """
    agent_id = arguments.get("agent_id") or os.environ.get("AGENTLOOM_AGENT_ID")
    if not agent_id:
        raise ValueError(
            "no agent id. Pass 'agent_id' or set AGENTLOOM_AGENT_ID in the MCP "
            "server environment."
        )
    operator_id = resolve_operator_id(arguments.get("operator_id"))
    return agent_id, operator_id, _resolve_workspace(arguments.get("workspace_key"))


def _resolve_lane(arguments: dict[str, Any]) -> str:
    """Resolve the lane a tool call acts on.

    Falls through to the same environment variable the CLI reads, so a checkout
    pinned to one lane answers consistently no matter which surface asks.
    """
    return resolve_lane(arguments.get("lane"))


def tool_session_search(arguments: dict[str, Any]) -> str:
    query = arguments.get("query", "").strip()
    if not query:
        return "Error: 'query' parameter is required."

    workspace_key = _resolve_workspace(arguments.get("workspace_key")) or None
    since = arguments.get("since")
    limit = int(arguments.get("limit", 8))
    lexical_only = bool(arguments.get("lexical_only", False))

    from agentloom_runtime.memory.embedding_provider import embed_query, get_embedding_model

    model = get_embedding_model()
    query_vec = None if lexical_only else embed_query(query, model=model)

    hits = store.search_archive(
        query,
        workspace_key=workspace_key,
        since=since,
        limit=limit,
        query_vec=query_vec,
        model=model,
    )
    if not hits:
        return f"No matching archived conversation snippets found for query: '{query}'"

    lines = [f"Found {len(hits)} matching conversation snippet(s) for query: '{query}'\n"]
    for i, h in enumerate(hits, 1):
        lines.append(
            f"### Hit {i} (Score: {h.score:.3f}, Granularity: {h.granularity})\n"
            f"- **Transcript Ref:** `{h.source_ref}`\n"
            f"- **Sequence Window:** `{h.seq_start}` to `{h.seq_end}`\n"
            f"- **Captured At:** {h.captured_at or 'unknown'}\n"
            f"- **Snippet:**\n> {h.snippet.replace(chr(10), ' ')}\n\n"
            f"*To read surrounding conversation turns, invoke tool `session_get_context` with:*\n"
            f"```json\n"
            f'{{"source_ref": "{h.source_ref}", "around_seq": {h.seq_start}}}\n'
            f"```\n"
        )
    return "\n".join(lines)


def tool_session_get_context(arguments: dict[str, Any]) -> str:
    around_seq = arguments.get("around_seq")
    if around_seq is None:
        return "Error: 'around_seq' parameter is required."
    try:
        around_seq = int(around_seq)
    except (TypeError, ValueError):
        return f"Error: invalid around_seq: {around_seq}"

    source_ref = arguments.get("source_ref")
    transcript_id = arguments.get("transcript_id")
    radius = int(arguments.get("radius", 8))
    fmt = arguments.get("format", "markdown")

    doc = store.load_transcript(
        transcript_id=transcript_id,
        source_ref=source_ref,
        workspace_key=_resolve_workspace(None) if not source_ref and not transcript_id else None,
    )
    if doc is None:
        return f"Transcript not found for reference: {source_ref or transcript_id}"

    sliced = doc.around(around_seq, radius=radius)
    if not sliced.turns:
        return f"No turns found in transcript around sequence {around_seq}."

    if fmt == "text":
        rendered = render_text(sliced)
    else:
        rendered = render_markdown(sliced)

    header = (
        f"# Archived Conversation Context\n"
        f"**Source Ref:** `{doc.source_ref}` (Host: `{doc.source_host}`)\n"
        f"**Turns Shown:** {len(sliced.turns)} around sequence {around_seq} (radius: {radius})\n"
        f"**Total Redactions:** {doc.redaction_count}\n\n"
        f"---\n\n"
    )
    return header + rendered


def tool_session_get_checkpoint(arguments: dict[str, Any]) -> str:
    session_id = arguments.get("session_id")
    if session_id:
        cps = store.list_checkpoints(session_id, limit=3)
        if not cps:
            return f"No checkpoints found for session: {session_id}"
        lines = [f"# Checkpoints for Session `{session_id}`\n"]
        for cp in cps:
            lines.append(
                f"### Checkpoint `{cp['checkpoint_id']}` ({cp['created_at']})\n"
                f"- **Next Action:** {cp.get('next_action') or 'None specified'}\n"
                f"- **Open Plan:** `{cp.get('open_plan_path') or 'None'}`\n"
                f"- **VCS Branch / Head:** `{cp.get('vcs_branch')}` / `{cp.get('vcs_head')}`\n"
                f"- **Decisions:** {json.dumps(cp.get('decisions') or [], ensure_ascii=False)}\n"
            )
        return "\n".join(lines)

    try:
        agent_id, operator_id, workspace_key = _resolve_identity(arguments)
    except ValueError as exc:
        return f"Error: {exc}"

    host = detect_host_context()
    lane = _resolve_lane(arguments)
    pack = store.resume(
        agent_id, operator_id, workspace_key, turn_limit=5, lane=lane, host=host
    )
    if pack is None:
        return (
            f"No active or parked session found for ({agent_id}, {operator_id}, "
            f"{workspace_key}) in lane '{lane}'."
        )

    return store.render_resume_pack(pack, current_host=host.host_hint)


def tool_session_get_lineage(arguments: dict[str, Any]) -> str:
    session_id = arguments.get("session_id")
    if not session_id:
        # An agent asking "how did I get here" has no session id to hand. Fall
        # back to the session its own identity already owns.
        try:
            agent_id, operator_id, workspace_key = _resolve_identity(arguments)
        except ValueError as exc:
            return f"Error: {exc}"
        pack = store.resume(
            agent_id, operator_id, workspace_key, turn_limit=0, lane=_resolve_lane(arguments)
        )
        if pack is None:
            return (
                f"No session found for ({agent_id}, {operator_id}, {workspace_key}). "
                "Pass 'session_id' explicitly to inspect another session's lineage."
            )
        session_id = pack.session.session_id

    try:
        lineage = store.get_session_lineage(session_id)
    except Exception as exc:
        return f"Error retrieving lineage: {exc}"

    cur = lineage["session"]
    lines = [
        f"# Session Lineage for `{cur['session_id']}`\n",
        f"- **Status:** `{cur['status']}`",
        f"- **Agent:** `{cur['agent_id']}`",
        f"- **Operator:** `{cur['operator_id']}`",
        f"- **Workspace:** `{cur['workspace_key']}`",
        f"- **Title:** {cur.get('title') or 'None'}",
    ]
    if cur.get("fork_reason"):
        lines.append(f"- **Fork Reason:** `{cur['fork_reason']}`")
    if cur.get("fork_checkpoint_id"):
        lines.append(f"- **Fork Checkpoint:** `{cur['fork_checkpoint_id']}`")

    ancestors = lineage.get("ancestors", [])
    if ancestors:
        lines.append("\n## Ancestor Sessions (Oldest to Direct Parent):")
        for i, anc in enumerate(reversed(ancestors), 1):
            lines.append(
                f"{i}. `{anc['session_id']}` [{anc['status']}] ({anc['agent_id']}) — "
                f"{anc.get('title') or 'Untitled'} (Created: {anc.get('created_at')})"
            )
    else:
        lines.append("\n## Ancestors: None (Root Session)")

    children = lineage.get("children", [])
    if children:
        lines.append("\n## Direct Child Forks:")
        for ch in children:
            lines.append(
                f"- `{ch['session_id']}` [{ch['status']}] ({ch['agent_id']}) "
                f"[Reason: `{ch.get('fork_reason') or 'unspecified'}`] — {ch.get('title') or 'Untitled'}"
            )
    else:
        lines.append("\n## Direct Child Forks: None")

    return "\n".join(lines)


TOOL_HANDLERS = {
    "session_search": tool_session_search,
    "session_get_context": tool_session_get_context,
    "session_get_checkpoint": tool_session_get_checkpoint,
    "session_get_lineage": tool_session_get_lineage,
}


def handle_request(req: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Handle a single MCP JSON-RPC 2.0 request."""
    method = req.get("method")
    req_id = req.get("id")

    # Notifications do not receive responses
    if req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": TOOLS,
            },
        }

    if method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error: unknown tool '{tool_name}'",
                        }
                    ],
                    "isError": True,
                },
            }

        try:
            output = handler(arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": output,
                        }
                    ],
                    "isError": False,
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Execution error in '{tool_name}': {exc}",
                        }
                    ],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


def run_stdio_server() -> int:
    """Run MCP server over stdio until EOF."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def main() -> int:
    return run_stdio_server()


if __name__ == "__main__":
    sys.exit(main())
