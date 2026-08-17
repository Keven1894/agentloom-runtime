"""Layer 0 working-session memory.

Read/write operations for cross-host session continuity. All lookups key on
``(agent_id, operator_id, workspace_key)``; nothing here reads editor-local
storage, and no query filters on a machine name, filesystem path, or IDE.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from agentloom_runtime.db import connect
from agentloom_runtime.session.identity import HostContext

__all__ = [
    "ResumePack",
    "SessionRecord",
    "add_turn",
    "checkpoint",
    "close_session",
    "list_checkpoints",
    "open_session",
    "park_session",
    "render_resume_pack",
    "resume",
    "search_sessions",
]

CHECKPOINT_SCHEMA_VERSION = 1

_SESSION_COLUMNS = (
    "session_id, agent_id, operator_id, workspace_key, status, title, "
    "workspace_path_hint, host_hint, ide_hint, created_at, updated_at, last_checkpoint_at"
)

_CHECKPOINT_COLUMNS = (
    "checkpoint_id, session_id, schema_version, created_at, host_hint, ide_hint, "
    "vcs_head, vcs_branch, vcs_status_summary, open_plan_path, next_action, "
    "decisions_json, transcript_citations_json, payload_json"
)


def _as_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _from_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


@dataclass
class SessionRecord:
    session_id: str
    agent_id: str
    operator_id: str
    workspace_key: str
    status: str
    title: Optional[str] = None
    workspace_path_hint: Optional[str] = None
    host_hint: Optional[str] = None
    ide_hint: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_checkpoint_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Any) -> "SessionRecord":
        return cls(
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            operator_id=row["operator_id"],
            workspace_key=row["workspace_key"],
            status=row["status"],
            title=row["title"],
            workspace_path_hint=row["workspace_path_hint"],
            host_hint=row["host_hint"],
            ide_hint=row["ide_hint"],
            created_at=_iso(row["created_at"]),
            updated_at=_iso(row["updated_at"]),
            last_checkpoint_at=_iso(row["last_checkpoint_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResumePack:
    """Everything an agent needs to continue work on another host."""

    session: SessionRecord
    checkpoint: Optional[dict[str, Any]] = None
    turns: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict(),
            "checkpoint": self.checkpoint,
            "turns": self.turns,
        }


def _fetch_session(conn: Any, session_id: str) -> Optional[SessionRecord]:
    row = conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM agent_sessions WHERE session_id = ?",
        [session_id],
    ).fetchone()
    return SessionRecord.from_row(row) if row else None


def _find_open(conn: Any, agent_id: str, operator_id: str, workspace_key: str):
    return conn.execute(
        f"""
        SELECT {_SESSION_COLUMNS} FROM agent_sessions
        WHERE agent_id = ? AND operator_id = ? AND workspace_key = ? AND status = 'open'
        LIMIT 1
        """,
        [agent_id, operator_id, workspace_key],
    ).fetchone()


def open_session(
    agent_id: str,
    operator_id: str,
    workspace_key: str,
    title: Optional[str] = None,
    host: Optional[HostContext] = None,
) -> tuple[SessionRecord, bool]:
    """Return the open session for this identity, creating it if absent.

    Returns ``(session, created)``. At most one open session can exist per
    identity; the database enforces this with a unique generated key, so a
    concurrent opener loses the insert and reads the winner's row instead.
    """
    conn = connect()
    try:
        existing = _find_open(conn, agent_id, operator_id, workspace_key)
        if existing:
            return SessionRecord.from_row(existing), False

        session_id = str(uuid.uuid4())
        try:
            conn.execute(
                """
                INSERT INTO agent_sessions
                    (session_id, agent_id, operator_id, workspace_key, status, title,
                     workspace_path_hint, host_hint, ide_hint)
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                [
                    session_id,
                    agent_id,
                    operator_id,
                    workspace_key,
                    title,
                    host.workspace_path_hint if host else None,
                    host.host_hint if host else None,
                    host.ide_hint if host else None,
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            existing = _find_open(conn, agent_id, operator_id, workspace_key)
            if existing:
                return SessionRecord.from_row(existing), False
            raise

        record = _fetch_session(conn, session_id)
        assert record is not None
        return record, True
    finally:
        conn.close()


def checkpoint(
    session_id: str,
    next_action: Optional[str] = None,
    open_plan_path: Optional[str] = None,
    vcs_head: Optional[str] = None,
    vcs_branch: Optional[str] = None,
    vcs_status_summary: Optional[str] = None,
    decisions: Optional[list[Any]] = None,
    transcript_citations: Optional[list[Any]] = None,
    payload: Optional[dict[str, Any]] = None,
    host: Optional[HostContext] = None,
) -> str:
    """Record a resume point for a session. Returns the checkpoint id."""
    checkpoint_id = str(uuid.uuid4())
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO session_checkpoints
                (checkpoint_id, session_id, schema_version, host_hint, ide_hint,
                 vcs_head, vcs_branch, vcs_status_summary, open_plan_path, next_action,
                 decisions_json, transcript_citations_json, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                checkpoint_id,
                session_id,
                CHECKPOINT_SCHEMA_VERSION,
                host.host_hint if host else None,
                host.ide_hint if host else None,
                vcs_head,
                vcs_branch,
                vcs_status_summary,
                open_plan_path,
                next_action,
                _as_json(decisions),
                _as_json(transcript_citations),
                _as_json(payload),
            ],
        )
        conn.execute(
            "UPDATE agent_sessions SET last_checkpoint_at = CURRENT_TIMESTAMP(3) "
            "WHERE session_id = ?",
            [session_id],
        )
        conn.commit()
        return checkpoint_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _checkpoint_from_row(row: Any) -> dict[str, Any]:
    return {
        "checkpoint_id": row["checkpoint_id"],
        "session_id": row["session_id"],
        "schema_version": row["schema_version"],
        "created_at": _iso(row["created_at"]),
        "host_hint": row["host_hint"],
        "ide_hint": row["ide_hint"],
        "vcs_head": row["vcs_head"],
        "vcs_branch": row["vcs_branch"],
        "vcs_status_summary": row["vcs_status_summary"],
        "open_plan_path": row["open_plan_path"],
        "next_action": row["next_action"],
        "decisions": _from_json(row["decisions_json"]),
        "transcript_citations": _from_json(row["transcript_citations_json"]),
        "payload": _from_json(row["payload_json"]),
    }


def list_checkpoints(session_id: str, limit: int = 10) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT {_CHECKPOINT_COLUMNS} FROM session_checkpoints
            WHERE session_id = ? ORDER BY created_at DESC, checkpoint_id DESC LIMIT ?
            """,
            [session_id, int(limit)],
        ).fetchall()
        return [_checkpoint_from_row(row) for row in rows]
    finally:
        conn.close()


def add_turn(session_id: str, role: str, summary: str) -> str:
    """Append a short turn summary. Never store full host transcripts here."""
    if role not in {"human", "agent", "system"}:
        raise ValueError(f"invalid role: {role}")
    turn_id = str(uuid.uuid4())
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM session_turns WHERE session_id = ?",
            [session_id],
        ).fetchone()
        seq = int(row["max_seq"]) + 1
        conn.execute(
            "INSERT INTO session_turns (turn_id, session_id, seq, role, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            [turn_id, session_id, seq, role, summary],
        )
        conn.commit()
        return turn_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _recent_turns(conn: Any, session_id: str, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    rows = conn.execute(
        "SELECT seq, role, summary, created_at FROM session_turns "
        "WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
        [session_id, int(limit)],
    ).fetchall()
    turns = [
        {
            "seq": row["seq"],
            "role": row["role"],
            "summary": row["summary"],
            "created_at": _iso(row["created_at"]),
        }
        for row in rows
    ]
    turns.reverse()
    return turns


def resume(
    agent_id: str,
    operator_id: str,
    workspace_key: str,
    turn_limit: int = 10,
) -> Optional[ResumePack]:
    """Return the resume pack for an identity, or ``None`` if there is nothing.

    Prefers the open session. If none is open, falls back to the most recently
    updated parked session so a deliberate pause can still be picked up.
    """
    conn = connect()
    try:
        row = _find_open(conn, agent_id, operator_id, workspace_key)
        if row is None:
            row = conn.execute(
                f"""
                SELECT {_SESSION_COLUMNS} FROM agent_sessions
                WHERE agent_id = ? AND operator_id = ? AND workspace_key = ?
                  AND status = 'parked'
                ORDER BY updated_at DESC LIMIT 1
                """,
                [agent_id, operator_id, workspace_key],
            ).fetchone()
        if row is None:
            return None

        session = SessionRecord.from_row(row)
        latest = conn.execute(
            f"""
            SELECT {_CHECKPOINT_COLUMNS} FROM session_checkpoints
            WHERE session_id = ? ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1
            """,
            [session.session_id],
        ).fetchone()

        return ResumePack(
            session=session,
            checkpoint=_checkpoint_from_row(latest) if latest else None,
            turns=_recent_turns(conn, session.session_id, turn_limit),
        )
    finally:
        conn.close()


def search_sessions(
    agent_id: Optional[str] = None,
    operator_id: Optional[str] = None,
    workspace_key: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> list[SessionRecord]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("agent_id", agent_id),
        ("operator_id", operator_id),
        ("workspace_key", workspace_key),
        ("status", status),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM agent_sessions {where} "
            "ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [SessionRecord.from_row(row) for row in rows]
    finally:
        conn.close()


def _set_status(session_id: str, status: str) -> bool:
    conn = connect()
    try:
        cursor = conn.execute(
            "UPDATE agent_sessions SET status = ? WHERE session_id = ?",
            [status, session_id],
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def park_session(session_id: str) -> bool:
    """Pause a session without ending it. Frees the identity's open slot."""
    return _set_status(session_id, "parked")


def close_session(session_id: str) -> bool:
    return _set_status(session_id, "closed")


def render_resume_pack(pack: Optional[ResumePack]) -> str:
    """Render a resume pack as plain text.

    Output is deliberately format-neutral so any agent, in any host, can read it
    straight out of a terminal without parsing a proprietary structure.
    """
    if pack is None:
        return "No previous session found for this identity. Starting fresh."

    session = pack.session
    lines = [
        "=== AgentLoom session resume ===",
        f"agent:     {session.agent_id}",
        f"operator:  {session.operator_id}",
        f"workspace: {session.workspace_key}",
        f"session:   {session.session_id} ({session.status})",
    ]
    if session.title:
        lines.append(f"title:     {session.title}")
    lines.append(f"updated:   {session.updated_at}")

    cp = pack.checkpoint
    if not cp:
        lines += ["", "No checkpoint recorded yet."]
    else:
        lines += ["", f"--- last checkpoint ({cp['created_at']}) ---"]
        origin = " / ".join(x for x in (cp.get("host_hint"), cp.get("ide_hint")) if x)
        if origin:
            lines.append(f"recorded on: {origin}")
        if cp.get("vcs_branch") or cp.get("vcs_head"):
            head = (cp.get("vcs_head") or "")[:12]
            lines.append(f"vcs:         {cp.get('vcs_branch') or '?'} @ {head or '?'}")
        if cp.get("open_plan_path"):
            lines.append(f"open plan:   {cp['open_plan_path']}")
        if cp.get("next_action"):
            lines += ["", "NEXT ACTION:", f"  {cp['next_action']}"]
        decisions = cp.get("decisions") or []
        if decisions:
            lines += ["", "decisions:"]
            lines += [f"  - {item}" for item in decisions]
        if cp.get("vcs_status_summary"):
            lines += ["", "working tree at checkpoint:", *(
                f"  {line}" for line in str(cp["vcs_status_summary"]).splitlines()
            )]
        citations = cp.get("transcript_citations") or []
        if citations:
            lines += ["", "transcript citations:"]
            lines += [f"  - {item}" for item in citations]

    if pack.turns:
        lines += ["", "--- recent turns ---"]
        lines += [f"  [{t['seq']}] {t['role']}: {t['summary']}" for t in pack.turns]

    return "\n".join(lines)
