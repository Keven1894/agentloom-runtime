"""Layer 0 working-session memory.

Read/write operations for cross-host session continuity. All lookups key on
``(agent_id, operator_id, workspace_key, lane)``; nothing here reads
editor-local storage, and no session lookup filters on a machine name,
filesystem path, or IDE.

Machine names appear in exactly two places, both advisory. They are written as
provenance on sessions and checkpoints, and they identify rows in
``session_hosts``, which records which machines are active in a session so a
human can be told that someone else is already working here. Neither reaches a
session-lookup predicate.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Optional

from agentloom_runtime.db import connect
from agentloom_runtime.session.identity import DEFAULT_LANE, HostContext
from agentloom_runtime.session.index import (
    ArchiveHit,
    apply_turn_overlay,
    chunk_document,
    decode_vector,
    encode_vector,
    hybrid_rank,
    snippet as make_snippet,
)
from agentloom_runtime.session.transcript import TranscriptDocument

__all__ = [
    "ArchiveHit",
    "LanesUnavailableError",
    "ResumePack",
    "SessionInUseError",
    "SessionOpenError",
    "SessionRecord",
    "TranscriptRecord",
    "add_turn",
    "checkpoint",
    "close_session",
    "list_session_hosts",
    "live_hosts",
    "touch_host",
    "get_session_lineage",
    "get_workspace_session_tree",
    "index_transcript",
    "compact_embeddings",
    "count_transcripts",
    "index_workspace",
    "list_checkpoints",
    "list_transcripts",
    "load_transcript",
    "get_transcript_record",
    "set_transcript_title",
    "set_transcript_presentation",
    "normalize_presentation",
    "open_session",
    "park_session",
    "render_resume_pack",
    "resume",
    "search_archive",
    "search_sessions",
    "store_transcript",
]

CHECKPOINT_SCHEMA_VERSION = 1

# How long a machine keeps counting as active in a session after its last
# recorded activity. The two ways of being wrong are not symmetric: calling a
# finished host "live" costs an unnecessary lane, while calling a working host
# "gone" invites the fork that parks it. So the window is generous, and there
# is an explicit override for the case it gets wrong.
DEFAULT_LIVE_WINDOW_MINUTES = 240

_SESSION_COLUMNS = (
    "session_id, agent_id, operator_id, workspace_key, lane, parent_session_id, "
    "fork_checkpoint_id, fork_reason, status, title, workspace_path_hint, "
    "host_hint, ide_hint, created_at, updated_at, last_checkpoint_at"
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


def as_naive_datetime(value: Any) -> Optional[datetime]:
    """Host file mtimes arrive as Unix timestamps; MySQL DATETIME is naive local."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    return None


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _col(row: Any, name: str, default: Any = None) -> Any:
    """Read a column that older rows, or hand-built test rows, may not carry."""
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _live_window_minutes() -> int:
    """Minutes of silence after which a machine stops counting as active."""
    import os

    raw = os.environ.get("AGENTLOOM_SESSION_LIVE_MINUTES")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_LIVE_WINDOW_MINUTES


class SessionOpenError(RuntimeError):
    """A session could not be opened, for a reason the operator can act on.

    Every subclass carries a message that names the next step, so a caller can
    print it verbatim instead of surfacing a database error about a generated
    column nobody at the keyboard has heard of.
    """


class LanesUnavailableError(SessionOpenError):
    """Raised when the database still keys open sessions without the lane."""


class SessionInUseError(SessionOpenError):
    """Raised when an operation would displace a session another host is using.

    Carries the live hosts so the caller can name them instead of making the
    operator go and look.
    """

    def __init__(self, session_id: str, hosts: list[dict[str, Any]]):
        self.session_id = session_id
        self.hosts = hosts
        where = ", ".join(
            f"{h.get('host')} (last seen {h.get('last_seen_at')})" for h in hosts
        )
        super().__init__(
            f"session {session_id} is still active on {where}. "
            "Forking would park it out from under that host."
        )


@dataclass
class SessionRecord:
    session_id: str
    agent_id: str
    operator_id: str
    workspace_key: str
    status: str
    lane: str = DEFAULT_LANE
    parent_session_id: Optional[str] = None
    fork_checkpoint_id: Optional[str] = None
    fork_reason: Optional[str] = None
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
            lane=_col(row, "lane") or DEFAULT_LANE,
            parent_session_id=_col(row, "parent_session_id"),
            fork_checkpoint_id=_col(row, "fork_checkpoint_id"),
            fork_reason=_col(row, "fork_reason"),
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
    # Other machines currently active in this session. Advisory: it changes
    # what the operator is asked, never which session was found.
    live_hosts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict(),
            "checkpoint": self.checkpoint,
            "turns": self.turns,
            "live_hosts": self.live_hosts,
        }


def _fetch_session(conn: Any, session_id: str) -> Optional[SessionRecord]:
    row = conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM agent_sessions WHERE session_id = ?",
        [session_id],
    ).fetchone()
    return SessionRecord.from_row(row) if row else None


def _find_open(
    conn: Any,
    agent_id: str,
    operator_id: str,
    workspace_key: str,
    lane: str = DEFAULT_LANE,
):
    return conn.execute(
        f"""
        SELECT {_SESSION_COLUMNS} FROM agent_sessions
        WHERE agent_id = ? AND operator_id = ? AND workspace_key = ? AND lane = ?
          AND status = 'open'
        LIMIT 1
        """,
        [agent_id, operator_id, workspace_key, lane],
    ).fetchone()


def _host_row(row: Any) -> dict[str, Any]:
    return {
        "host": row["host"],
        "ide": row["ide"],
        "first_seen_at": _iso(row["first_seen_at"]),
        "last_seen_at": _iso(row["last_seen_at"]),
    }


def _touch_host(conn: Any, session_id: str, host: HostContext) -> None:
    """Record that this machine is active in this session, as of now.

    ``last_seen_at`` is written explicitly rather than by ON UPDATE, so a row
    only moves when a host actually does something — the lesson 012 learned
    about letting the server stamp a column behind the application's back.
    """
    conn.execute(
        """
        INSERT INTO session_hosts (session_id, host, ide, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP(3), CURRENT_TIMESTAMP(3))
        ON DUPLICATE KEY UPDATE ide = ?, last_seen_at = CURRENT_TIMESTAMP(3)
        """,
        [session_id, host.host_hint, host.ide_hint, host.ide_hint],
    )
    conn.commit()


def _live_hosts(
    conn: Any,
    session_id: str,
    exclude_host: Optional[str] = None,
    within_minutes: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Machines recently active in this session, newest first.

    The freshness arithmetic runs server-side on purpose. Comparing a
    server-written timestamp against the calling machine's clock would make the
    answer depend on how well two hosts agree about the time, which is exactly
    the kind of skew this layer exists to tolerate.
    """
    window = int(_live_window_minutes() if within_minutes is None else within_minutes)
    rows = conn.execute(
        f"""
        SELECT host, ide, first_seen_at, last_seen_at FROM session_hosts
        WHERE session_id = ? AND last_seen_at >= (NOW(3) - INTERVAL {window} MINUTE)
        ORDER BY last_seen_at DESC
        """,
        [session_id],
    ).fetchall()
    return [_host_row(r) for r in rows if not exclude_host or r["host"] != exclude_host]


def _implied_activity(
    conn: Any,
    session_id: str,
    exclude_host: Optional[str] = None,
    within_minutes: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Recent activity inferred from the session row, for hosts with no heartbeat.

    ``session_hosts`` is only written by lane-aware code, so during a rollout —
    or against any host still running an older release — it is empty, and a
    guard that trusted it alone would wave through exactly the case it exists
    to stop. The session's own ``host_hint`` and timestamps are written by every
    version, which makes them the floor this can never fall below.

    Used only to refuse a destructive action, never to resolve a session.
    """
    window = int(_live_window_minutes() if within_minutes is None else within_minutes)
    row = conn.execute(
        f"""
        SELECT host_hint, ide_hint,
               GREATEST(COALESCE(last_checkpoint_at, updated_at), updated_at) AS seen_at
        FROM agent_sessions
        WHERE session_id = ?
          AND GREATEST(COALESCE(last_checkpoint_at, updated_at), updated_at)
              >= (NOW(3) - INTERVAL {window} MINUTE)
        """,
        [session_id],
    ).fetchone()
    if not row or not row["host_hint"]:
        return []
    if exclude_host and row["host_hint"] == exclude_host:
        return []
    return [
        {
            "host": row["host_hint"],
            "ide": row["ide_hint"],
            "first_seen_at": None,
            "last_seen_at": _iso(row["seen_at"]),
        }
    ]


def touch_host(session_id: str, host: HostContext) -> None:
    """Record this machine's activity in a session."""
    conn = connect()
    try:
        _touch_host(conn, session_id, host)
    finally:
        conn.close()


def live_hosts(
    session_id: str,
    exclude_host: Optional[str] = None,
    within_minutes: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Machines recently active in a session, excluding one if asked."""
    conn = connect()
    try:
        return _live_hosts(conn, session_id, exclude_host, within_minutes)
    finally:
        conn.close()


def list_session_hosts(session_id: str) -> list[dict[str, Any]]:
    """Every machine ever seen in a session, newest activity first."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT host, ide, first_seen_at, last_seen_at FROM session_hosts
            WHERE session_id = ? ORDER BY last_seen_at DESC
            """,
            [session_id],
        ).fetchall()
        return [_host_row(r) for r in rows]
    finally:
        conn.close()


def open_session(
    agent_id: str,
    operator_id: str,
    workspace_key: str,
    title: Optional[str] = None,
    host: Optional[HostContext] = None,
    parent_session_id: Optional[str] = None,
    fork_checkpoint_id: Optional[str] = None,
    fork_reason: Optional[str] = None,
    lane: str = DEFAULT_LANE,
    force: bool = False,
) -> tuple[SessionRecord, bool]:
    """Return the open session for this identity and lane, creating it if absent.

    Returns ``(session, created)``. At most one open session can exist per
    identity and lane; the database enforces this with a unique generated key,
    so a concurrent opener loses the insert and reads the winner's row instead.

    Forking parks the session it forks from, which is correct for a handoff and
    destructive during concurrent work. When another machine is still active in
    that session this raises :class:`SessionInUseError` rather than parking it;
    ``force`` overrides, and picking a different ``lane`` avoids the collision
    altogether.
    """
    conn = connect()
    try:
        existing = _find_open(conn, agent_id, operator_id, workspace_key, lane)
        if existing and parent_session_id:
            if not force:
                mine = host.host_hint if host else None
                intruders = _live_hosts(
                    conn, existing["session_id"], exclude_host=mine
                ) or _implied_activity(conn, existing["session_id"], exclude_host=mine)
                if intruders:
                    raise SessionInUseError(existing["session_id"], intruders)
            conn.execute(
                "UPDATE agent_sessions SET status = 'parked' WHERE session_id = ?",
                [existing["session_id"]],
            )
            conn.commit()
            existing = None

        if existing:
            if host:
                _touch_host(conn, existing["session_id"], host)
            return SessionRecord.from_row(existing), False

        if parent_session_id and not fork_checkpoint_id:
            cp_row = conn.execute(
                "SELECT checkpoint_id FROM session_checkpoints "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                [parent_session_id],
            ).fetchone()
            if cp_row:
                fork_checkpoint_id = cp_row["checkpoint_id"]
            if not fork_reason:
                fork_reason = "continuation"

        session_id = str(uuid.uuid4())
        try:
            conn.execute(
                """
                INSERT INTO agent_sessions
                    (session_id, agent_id, operator_id, workspace_key, lane,
                     parent_session_id, fork_checkpoint_id, fork_reason,
                     status, title, workspace_path_hint, host_hint, ide_hint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                [
                    session_id,
                    agent_id,
                    operator_id,
                    workspace_key,
                    lane,
                    parent_session_id,
                    fork_checkpoint_id,
                    fork_reason,
                    title,
                    host.workspace_path_hint if host else None,
                    host.host_hint if host else None,
                    host.ide_hint if host else None,
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            existing = _find_open(conn, agent_id, operator_id, workspace_key, lane)
            if existing:
                return SessionRecord.from_row(existing), False
            # Losing the insert with no open session in *this* lane means the
            # unique key still spans only (agent, operator, workspace): the
            # database is mid-rollout, with 016 applied and 017 not. Say that,
            # rather than surfacing a bare integrity error from a generated
            # column nobody reading this call has heard of.
            other_lane = conn.execute(
                "SELECT lane FROM agent_sessions WHERE agent_id = ? AND operator_id = ? "
                "AND workspace_key = ? AND status = 'open' LIMIT 1",
                [agent_id, operator_id, workspace_key],
            ).fetchone()
            if other_lane and other_lane["lane"] != lane:
                raise LanesUnavailableError(
                    f"cannot open lane '{lane}': this database still allows only one "
                    f"open session per identity, and lane '{other_lane['lane']}' holds "
                    "it. Apply migration 017 (agentloom-session init) once every host "
                    "runs lane-aware code."
                ) from None
            raise

        record = _fetch_session(conn, session_id)
        assert record is not None
        if host:
            _touch_host(conn, session_id, host)
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
        if host:
            _touch_host(conn, session_id, host)
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


def add_turn(
    session_id: str,
    role: str,
    summary: str,
    host: Optional[HostContext] = None,
) -> str:
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
        if host:
            _touch_host(conn, session_id, host)
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
    lane: str = DEFAULT_LANE,
    host: Optional[HostContext] = None,
) -> Optional[ResumePack]:
    """Return the resume pack for an identity and lane, or ``None`` if empty.

    Prefers the open session. If none is open, falls back to the most recently
    updated parked session in the same lane, so a deliberate pause can still be
    picked up.

    Passing ``host`` records this machine as active in the session, which is
    what later lets another machine be told the lane is already occupied.
    """
    conn = connect()
    try:
        row = _find_open(conn, agent_id, operator_id, workspace_key, lane)
        if row is None:
            row = conn.execute(
                f"""
                SELECT {_SESSION_COLUMNS} FROM agent_sessions
                WHERE agent_id = ? AND operator_id = ? AND workspace_key = ?
                  AND lane = ? AND status = 'parked'
                ORDER BY updated_at DESC LIMIT 1
                """,
                [agent_id, operator_id, workspace_key, lane],
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

        # Read liveness before recording our own, or this host shows up as its
        # own company on the very first resume.
        others = _live_hosts(
            conn, session.session_id, exclude_host=host.host_hint if host else None
        )
        if host and session.status == "open":
            _touch_host(conn, session.session_id, host)

        return ResumePack(
            session=session,
            checkpoint=_checkpoint_from_row(latest) if latest else None,
            turns=_recent_turns(conn, session.session_id, turn_limit),
            live_hosts=others,
        )
    finally:
        conn.close()


def search_sessions(
    agent_id: Optional[str] = None,
    operator_id: Optional[str] = None,
    workspace_key: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    lane: Optional[str] = None,
) -> list[SessionRecord]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("agent_id", agent_id),
        ("operator_id", operator_id),
        ("workspace_key", workspace_key),
        ("status", status),
        ("lane", lane),
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


def get_session_lineage(session_id: str) -> dict[str, Any]:
    """Retrieve ancestor chain, current session, and direct child sessions."""
    conn = connect()
    try:
        current = _fetch_session(conn, session_id)
        if current is None:
            raise ValueError(f"session not found: {session_id}")

        ancestors: list[dict[str, Any]] = []
        curr_parent_id = current.parent_session_id
        visited = {session_id}
        while curr_parent_id and curr_parent_id not in visited:
            visited.add(curr_parent_id)
            parent = _fetch_session(conn, curr_parent_id)
            if parent is None:
                break
            ancestors.append(parent.to_dict())
            curr_parent_id = parent.parent_session_id

        child_rows = conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS} FROM agent_sessions
            WHERE parent_session_id = ?
            ORDER BY created_at ASC
            """,
            [session_id],
        ).fetchall()
        children = [SessionRecord.from_row(r).to_dict() for r in child_rows]

        return {
            "session": current.to_dict(),
            "ancestors": ancestors,
            "children": children,
        }
    finally:
        conn.close()


def get_workspace_session_tree(workspace_key: str) -> list[dict[str, Any]]:
    """Return the hierarchical DAG of all sessions in a workspace."""
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT {_SESSION_COLUMNS} FROM agent_sessions
            WHERE workspace_key = ?
            ORDER BY created_at ASC
            """,
            [workspace_key],
        ).fetchall()
        sessions = [SessionRecord.from_row(r).to_dict() for r in rows]

        session_map: dict[str, dict[str, Any]] = {
            s["session_id"]: {**s, "children": []} for s in sessions
        }
        roots: list[dict[str, Any]] = []
        for s in sessions:
            sid = s["session_id"]
            pid = s.get("parent_session_id")
            if pid and pid in session_map:
                session_map[pid]["children"].append(session_map[sid])
            else:
                roots.append(session_map[sid])
        return roots
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transcript archive
# ---------------------------------------------------------------------------

_TRANSCRIPT_COLUMNS = (
    "transcript_id, session_id, source_host, source_ref, workspace_key, agent_id, "
    "operator_id, title, title_source, presentation_json, captured_at, turn_count, "
    "redaction_count, body_bytes, content_sha256"
)

TITLE_MAX_CHARS = 255
TITLE_SOURCE_DERIVED = "derived"
TITLE_SOURCE_USER = "user"
PRESENTATION_LOCALES = ("original", "en", "es")
PRESENTATION_FIELDS = ("title", "description")


def _parse_presentation(value: Any) -> Optional[dict[str, Any]]:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        if key in row:
            return row[key]
    except TypeError:
        pass
    return default


def normalize_presentation(payload: Any) -> dict[str, Any]:
    """Keep title/description × original/en/es, plus optional translated turns."""
    if not isinstance(payload, dict):
        raise ValueError("presentation must be an object")
    out: dict[str, Any] = {}
    for field in PRESENTATION_FIELDS:
        raw = payload.get(field) or {}
        if not isinstance(raw, dict):
            continue
        loc = {
            locale: " ".join(str(raw[locale]).split())
            for locale in PRESENTATION_LOCALES
            if raw.get(locale)
        }
        if loc:
            out[field] = loc
    turns_raw = payload.get("turns")
    if isinstance(turns_raw, dict):
        cleaned_turns: dict[str, dict[str, list[str]]] = {}
        for locale, by_seq in turns_raw.items():
            if locale not in ("en", "es") or not isinstance(by_seq, dict):
                continue
            loc_map: dict[str, list[str]] = {}
            for seq, blocks in by_seq.items():
                if not isinstance(blocks, list):
                    continue
                texts = [str(b) for b in blocks]
                if texts:
                    loc_map[str(seq)] = texts
            if loc_map:
                cleaned_turns[str(locale)] = loc_map
        if cleaned_turns:
            out["turns"] = cleaned_turns
    if not out:
        raise ValueError("presentation needs at least one title, description, or translated turn")
    return out


def normalize_transcript_title(text: Optional[str]) -> Optional[str]:
    """Collapse whitespace and cap length. Empty becomes None (reset)."""
    if text is None:
        return None
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return None
    return cleaned[:TITLE_MAX_CHARS]


def next_archive_title(
    derived: Optional[str],
    existing_title: Optional[str],
    existing_source: Optional[str],
) -> tuple[Optional[str], str]:
    """Choose the title written on an archive upsert.

    A user rename outlives the next capture. Derived titles follow the body.
    """
    if existing_source == TITLE_SOURCE_USER and existing_title:
        return existing_title, TITLE_SOURCE_USER
    return derived, TITLE_SOURCE_DERIVED


@dataclass
class TranscriptRecord:
    """Archive metadata. The body is fetched separately — it is large."""

    transcript_id: str
    session_id: Optional[str]
    source_host: str
    source_ref: str
    workspace_key: str
    agent_id: Optional[str] = None
    operator_id: Optional[str] = None
    title: Optional[str] = None
    title_source: str = TITLE_SOURCE_DERIVED
    presentation: Optional[dict[str, Any]] = None
    captured_at: Optional[str] = None
    turn_count: int = 0
    redaction_count: int = 0
    body_bytes: int = 0
    content_sha256: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "TranscriptRecord":
        return cls(
            transcript_id=row["transcript_id"],
            session_id=row["session_id"],
            source_host=row["source_host"],
            source_ref=row["source_ref"],
            workspace_key=row["workspace_key"],
            agent_id=row["agent_id"],
            operator_id=row["operator_id"],
            title=row["title"],
            title_source=row["title_source"] or TITLE_SOURCE_DERIVED,
            presentation=_parse_presentation(_row_value(row, "presentation_json")),
            captured_at=_iso(row["captured_at"]),
            turn_count=int(row["turn_count"]),
            redaction_count=int(row["redaction_count"]),
            body_bytes=int(row["body_bytes"]),
            content_sha256=row["content_sha256"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def store_transcript(
    doc: TranscriptDocument,
    workspace_key: str,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    operator_id: Optional[str] = None,
    occurred_at: Any = None,
) -> tuple[str, bool]:
    """Archive a normalized transcript. Returns ``(transcript_id, changed)``.

    Conversations grow while they happen, so re-archiving is expected and
    updates the existing row for that ``(source_host, source_ref)``. An
    unchanged body is detected by hash and skips the blob write, which keeps
    repeated archiving cheap enough to run on every checkpoint.

    ``occurred_at`` is when the host last wrote the conversation (file mtime).
    That is what the listing date is. Without it, a bulk import stamps every
    row with the same ingest second.
    """
    body = json.dumps(doc.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    compressed = zlib.compress(body, 6)
    derived_title = normalize_transcript_title(doc.title)
    when = as_naive_datetime(occurred_at)

    conn = connect()
    try:
        existing = conn.execute(
            "SELECT transcript_id, content_sha256, title, title_source FROM session_transcripts "
            "WHERE source_host = ? AND source_ref = ?",
            [doc.source_host, doc.source_ref],
        ).fetchone()

        if existing:
            transcript_id = existing["transcript_id"]
            title, title_source = next_archive_title(
                derived_title, existing["title"], existing["title_source"]
            )
            if existing["content_sha256"] == digest:
                if when is not None:
                    _stamp_captured_at(conn, transcript_id, when)
                    conn.commit()
                return transcript_id, False
            conn.execute(
                """
                UPDATE session_transcripts
                SET session_id = COALESCE(?, session_id),
                    workspace_key = ?, agent_id = ?, operator_id = ?,
                    title = ?, title_source = ?,
                    turn_count = ?, redaction_count = ?, body_bytes = ?,
                    content_sha256 = ?, body_zlib = ?,
                    captured_at = COALESCE(?, captured_at)
                WHERE transcript_id = ?
                """,
                [
                    session_id,
                    workspace_key,
                    agent_id,
                    operator_id,
                    title,
                    title_source,
                    doc.turn_count,
                    doc.redaction_count,
                    len(body),
                    digest,
                    compressed,
                    when,
                    transcript_id,
                ],
            )
            if when is not None:
                _stamp_chunk_captured_at(conn, transcript_id, when)
        else:
            transcript_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO session_transcripts
                    (transcript_id, session_id, source_host, source_ref, workspace_key,
                     agent_id, operator_id, title, title_source, turn_count, redaction_count,
                     body_bytes, content_sha256, body_zlib, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP(3)))
                """,
                [
                    transcript_id,
                    session_id,
                    doc.source_host,
                    doc.source_ref,
                    workspace_key,
                    agent_id,
                    operator_id,
                    derived_title,
                    TITLE_SOURCE_DERIVED,
                    doc.turn_count,
                    doc.redaction_count,
                    len(body),
                    digest,
                    compressed,
                    when,
                ],
            )
        conn.commit()
        return transcript_id, True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _stamp_captured_at(conn, transcript_id: str, when: datetime) -> None:
    conn.execute(
        "UPDATE session_transcripts SET captured_at = ? WHERE transcript_id = ?",
        [when, transcript_id],
    )
    _stamp_chunk_captured_at(conn, transcript_id, when)


def _stamp_chunk_captured_at(conn, transcript_id: str, when: datetime) -> None:
    conn.execute(
        "UPDATE session_transcript_chunks SET captured_at = ? WHERE transcript_id = ?",
        [when, transcript_id],
    )


def count_transcripts(
    workspace_key: Optional[str] = None,
    session_id: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> int:
    """Return the total number of archived transcripts matching the filters."""
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("workspace_key", workspace_key),
        ("session_id", session_id),
        ("source_ref", source_ref),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    conn = connect()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM session_transcripts {where}",
            params,
        ).fetchone()
        if not row:
            return 0
        return int(row[0])
    finally:
        conn.close()


def list_transcripts(
    workspace_key: Optional[str] = None,
    session_id: Optional[str] = None,
    source_ref: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[TranscriptRecord]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("workspace_key", workspace_key),
        ("session_id", session_id),
        ("source_ref", source_ref),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    if offset > 0:
        params.append(int(offset))
        pagination = "LIMIT ? OFFSET ?"
    else:
        pagination = "LIMIT ?"

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_TRANSCRIPT_COLUMNS} FROM session_transcripts {where} "
            f"ORDER BY captured_at DESC {pagination}",
            params,
        ).fetchall()
        return [TranscriptRecord.from_row(row) for row in rows]
    finally:
        conn.close()


def get_transcript_record(transcript_id: str) -> Optional[TranscriptRecord]:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_TRANSCRIPT_COLUMNS} FROM session_transcripts WHERE transcript_id = ?",
            [transcript_id],
        ).fetchone()
        return TranscriptRecord.from_row(row) if row else None
    finally:
        conn.close()


def set_transcript_title(transcript_id: str, title: Optional[str]) -> TranscriptRecord:
    """Rename an archived conversation, or clear the name to restore the derived one.

    The compressed body is left alone: a rename is metadata, and rewriting the
    archive just to change a heading would look like the conversation changed.
    ``captured_at`` is pinned in the UPDATE so MySQL's ``ON UPDATE CURRENT_TIMESTAMP``
    does not float a renamed conversation to the top of the list.
    """
    wanted = normalize_transcript_title(title)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_TRANSCRIPT_COLUMNS}, body_zlib FROM session_transcripts "
            "WHERE transcript_id = ?",
            [transcript_id],
        ).fetchone()
        if row is None:
            raise KeyError(transcript_id)

        if wanted is None:
            derived = None
            if row["body_zlib"] is not None:
                payload = json.loads(zlib.decompress(row["body_zlib"]).decode("utf-8"))
                derived = normalize_transcript_title(
                    TranscriptDocument.from_dict(payload).title
                )
            conn.execute(
                "UPDATE session_transcripts SET title = ?, title_source = ?, "
                "captured_at = captured_at WHERE transcript_id = ?",
                [derived, TITLE_SOURCE_DERIVED, transcript_id],
            )
        else:
            conn.execute(
                "UPDATE session_transcripts SET title = ?, title_source = ?, "
                "captured_at = captured_at WHERE transcript_id = ?",
                [wanted, TITLE_SOURCE_USER, transcript_id],
            )
        conn.commit()
        updated = conn.execute(
            f"SELECT {_TRANSCRIPT_COLUMNS} FROM session_transcripts WHERE transcript_id = ?",
            [transcript_id],
        ).fetchone()
        return TranscriptRecord.from_row(updated)
    except KeyError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_transcript_presentation(transcript_id: str, payload: Any) -> TranscriptRecord:
    """Store a trilingual listing pack without rewriting the conversation body.

    ``captured_at`` is pinned so a summarizer write does not float the row.
    Re-archiving does not touch this column.
    """
    pack = normalize_presentation(payload)
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_TRANSCRIPT_COLUMNS} FROM session_transcripts WHERE transcript_id = ?",
            [transcript_id],
        ).fetchone()
        if row is None:
            raise KeyError(transcript_id)
        conn.execute(
            "UPDATE session_transcripts SET presentation_json = ?, "
            "captured_at = captured_at WHERE transcript_id = ?",
            [_as_json(pack), transcript_id],
        )
        conn.commit()
        updated = conn.execute(
            f"SELECT {_TRANSCRIPT_COLUMNS} FROM session_transcripts WHERE transcript_id = ?",
            [transcript_id],
        ).fetchone()
        return TranscriptRecord.from_row(updated)
    except KeyError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_transcript(
    transcript_id: Optional[str] = None,
    source_ref: Optional[str] = None,
    workspace_key: Optional[str] = None,
) -> Optional[TranscriptDocument]:
    """Fetch and decompress one archived conversation.

    With neither identifier, returns the most recent transcript for the
    workspace — the common "show me what we were just doing" case.
    """
    if transcript_id:
        where, params = "WHERE transcript_id = ?", [transcript_id]
    elif source_ref:
        where, params = "WHERE source_ref = ?", [source_ref]
    elif workspace_key:
        where, params = "WHERE workspace_key = ?", [workspace_key]
    else:
        raise ValueError("one of transcript_id, source_ref, or workspace_key is required")

    conn = connect()
    try:
        row = conn.execute(
            f"SELECT body_zlib FROM session_transcripts {where} "
            "ORDER BY captured_at DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None or row["body_zlib"] is None:
            return None
        payload = json.loads(zlib.decompress(row["body_zlib"]).decode("utf-8"))
        return TranscriptDocument.from_dict(payload)
    finally:
        conn.close()


def _parse_embedding(value: Any) -> Optional[list[float]]:
    if value is None:
        return None
    if isinstance(value, list):
        return [float(x) for x in value]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        if isinstance(parsed, list):
            return [float(x) for x in parsed]
    return None


@lru_cache(maxsize=None)
def _has_legacy_embedding_column() -> bool:
    """Whether the pre-009 JSON embedding column is still present.

    Cached: the answer only changes when a migration runs, which does not happen
    inside a live process, and the alternative is an information_schema lookup
    on the read path of every search.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name = 'session_transcript_chunks' "
            "  AND column_name = 'embedding'"
        ).fetchone()
        return bool(row and int(row["n"]))
    except Exception:
        return False
    finally:
        conn.close()


def _fill_legacy_embeddings(items: list[dict[str, Any]]) -> None:
    """Supply vectors for rows that predate the compact format.

    Dropping such rows would quietly shrink the vector half of the ranking
    while still reporting a hybrid search, so they are fetched — but only the
    rows that need it, and only while the legacy column still exists. After
    migration 009 the column is gone and this is a no-op.

    Run ``agentloom-session compact`` to make this path unnecessary.
    """
    if not _has_legacy_embedding_column():
        return
    missing = [item["id"] for item in items if not item.get("embedding")]
    if not missing:
        return

    placeholders = ",".join("?" * len(missing))
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT chunk_id, embedding FROM session_transcript_chunks "
            f"WHERE chunk_id IN ({placeholders})",
            missing,
        ).fetchall()
    finally:
        conn.close()

    legacy = {row["chunk_id"]: _parse_embedding(row["embedding"]) for row in rows}
    for item in items:
        if not item.get("embedding"):
            item["embedding"] = legacy.get(item["id"])


def _load_transcript_row(
    transcript_id: Optional[str] = None,
    source_ref: Optional[str] = None,
    workspace_key: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    if transcript_id:
        where, params = "WHERE transcript_id = ?", [transcript_id]
    elif source_ref:
        where, params = "WHERE source_ref = ?", [source_ref]
    elif workspace_key:
        where, params = "WHERE workspace_key = ?", [workspace_key]
    else:
        raise ValueError("one of transcript_id, source_ref, or workspace_key is required")

    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_TRANSCRIPT_COLUMNS}, body_zlib FROM session_transcripts {where} "
            "ORDER BY captured_at DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def index_transcript(
    transcript_id: str,
    *,
    model: str,
    embed_fn: Optional[Any] = None,
) -> dict[str, int]:
    """Chunk one archived conversation and upsert its index rows.

    ``embed_fn`` maps a list of strings to a list of vectors. When omitted,
    chunks are stored with NULL embeddings and lexical search still works.
    Re-indexing is cheap: unchanged content hashes skip both rewrite and embed.

    When ``presentation_json`` has English or Spanish turn overlays, those
    locales are chunked and embedded as extra rows (same table, ``locale``
    column). The archive body stays original; a query in another language
    ranks the overlay rows.
    """
    row = _load_transcript_row(transcript_id=transcript_id)
    if row is None or row.get("body_zlib") is None:
        return {"chunks": 0, "embedded": 0, "unchanged": 0}

    doc = TranscriptDocument.from_dict(
        json.loads(zlib.decompress(row["body_zlib"]).decode("utf-8"))
    )
    presentation = _parse_presentation(_row_value(row, "presentation_json"))
    chunks = list(chunk_document(doc, locale="original"))
    if presentation:
        titles = presentation.get("title") or {}
        descriptions = presentation.get("description") or {}
        overlays = presentation.get("turns") or {}
        for loc in ("en", "es"):
            overlay = overlays.get(loc)
            if not overlay:
                continue
            heading = " ".join(
                part for part in (titles.get(loc), descriptions.get(loc)) if part
            )
            chunks.extend(
                chunk_document(
                    apply_turn_overlay(doc, overlay),
                    heading=heading or None,
                    locale=loc,
                )
            )
    captured_at = row["captured_at"]
    workspace_key = row["workspace_key"]
    source_host = row["source_host"]
    source_ref = row["source_ref"]

    conn = connect()
    try:
        # Test for a vector's presence rather than selecting it: re-indexing
        # only needs to know whether one exists, and pulling the column here
        # would move the whole archive's embeddings for a no-op run.
        existing_rows = conn.execute(
            "SELECT chunk_id, locale, granularity, seq_start, seq_end, content_sha256, "
            "       (embedding_f32 IS NOT NULL) AS has_vector "
            "FROM session_transcript_chunks "
            "WHERE transcript_id = ? AND embedding_model = ?",
            [transcript_id, model],
        ).fetchall()
        existing = {
            (
                r.get("locale") or "original",
                r["granularity"],
                int(r["seq_start"]),
                int(r["seq_end"]),
            ): r
            for r in existing_rows
        }

        wanted_keys = {(c.locale, c.granularity, c.seq_start, c.seq_end) for c in chunks}
        stale = [r["chunk_id"] for key, r in existing.items() if key not in wanted_keys]
        if stale:
            placeholders = ",".join("?" * len(stale))
            conn.execute(
                f"DELETE FROM session_transcript_chunks WHERE chunk_id IN ({placeholders})",
                stale,
            )

        to_embed: list[tuple[str, str]] = []  # (chunk_id, content)
        unchanged = 0
        for chunk in chunks:
            key = (chunk.locale, chunk.granularity, chunk.seq_start, chunk.seq_end)
            prev = existing.get(key)
            if prev and prev["content_sha256"] == chunk.content_sha256:
                has_vec = bool(prev["has_vector"])
                if has_vec or embed_fn is None:
                    unchanged += 1
                    continue
                to_embed.append((prev["chunk_id"], chunk.content))
                continue

            chunk_id = prev["chunk_id"] if prev else str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO session_transcript_chunks
                    (chunk_id, transcript_id, workspace_key, source_host, source_ref,
                     granularity, locale, seq_start, seq_end, captured_at, content,
                     content_sha256, embedding_f32, embedding_dim,
                     embedding_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON DUPLICATE KEY UPDATE
                    workspace_key = VALUES(workspace_key),
                    source_host = VALUES(source_host),
                    source_ref = VALUES(source_ref),
                    captured_at = VALUES(captured_at),
                    content = VALUES(content),
                    content_sha256 = VALUES(content_sha256),
                    embedding_f32 = NULL,
                    embedding_dim = NULL
                """,
                [
                    chunk_id,
                    transcript_id,
                    workspace_key,
                    source_host,
                    source_ref,
                    chunk.granularity,
                    chunk.locale,
                    chunk.seq_start,
                    chunk.seq_end,
                    captured_at,
                    chunk.content,
                    chunk.content_sha256,
                    model,
                ],
            )
            to_embed.append((chunk_id, chunk.content))

        embedded = 0
        if embed_fn and to_embed:
            vectors = embed_fn([content for _, content in to_embed])
            if len(vectors) != len(to_embed):
                raise RuntimeError(
                    f"embed_fn returned {len(vectors)} vectors for {len(to_embed)} chunks"
                )
            for (chunk_id, _), vector in zip(to_embed, vectors):
                conn.execute(
                    "UPDATE session_transcript_chunks "
                    "SET embedding_f32 = ?, embedding_dim = ? WHERE chunk_id = ?",
                    [encode_vector(vector), len(vector) if vector else None, chunk_id],
                )
                embedded += 1

        conn.commit()
        return {
            "chunks": len(chunks),
            "embedded": embedded,
            "unchanged": unchanged,
            "deleted": len(stale),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def compact_embeddings(
    *,
    workspace_key: Optional[str] = None,
    batch_size: int = 200,
) -> dict[str, int]:
    """Convert legacy JSON embeddings to the compact float32 column.

    A pure re-encoding: the vectors are identical, so this costs no embedding
    API calls and can run repeatedly. Rows are processed in batches because the
    JSON column is the very thing that makes a full scan expensive.

    Once migration 009 has dropped the JSON column there is nothing left to
    convert, and this reports zero rather than failing — the command stays safe
    to leave in a scheduled job.
    """
    stats = {"scanned": 0, "converted": 0, "unreadable": 0}
    if not _has_legacy_embedding_column():
        return stats

    where = "embedding IS NOT NULL AND embedding_f32 IS NULL"
    params: list[Any] = []
    if workspace_key:
        where += " AND workspace_key = ?"
        params.append(workspace_key)

    conn = connect()
    try:
        while True:
            rows = conn.execute(
                f"SELECT chunk_id, embedding FROM session_transcript_chunks "
                f"WHERE {where} LIMIT {int(batch_size)}",
                params,
            ).fetchall()
            if not rows:
                break
            for row in rows:
                stats["scanned"] += 1
                vector = _parse_embedding(row["embedding"])
                if not vector:
                    # Leave it for inspection rather than silently blanking a row.
                    stats["unreadable"] += 1
                    continue
                conn.execute(
                    "UPDATE session_transcript_chunks "
                    "SET embedding_f32 = ?, embedding_dim = ? WHERE chunk_id = ?",
                    [encode_vector(vector), len(vector), row["chunk_id"]],
                )
                stats["converted"] += 1
            conn.commit()
            if stats["unreadable"] and stats["converted"] == 0:
                break  # nothing convertible left; avoid looping on bad rows
    finally:
        conn.close()
    return stats


def index_workspace(
    workspace_key: str,
    *,
    model: str,
    embed_fn: Optional[Any] = None,
    limit: int = 0,
) -> dict[str, int]:
    """Index every archived conversation for a workspace."""
    records = list_transcripts(workspace_key=workspace_key, limit=limit or 10_000)
    totals = {"transcripts": 0, "chunks": 0, "embedded": 0, "unchanged": 0, "deleted": 0}
    for record in records:
        stats = index_transcript(record.transcript_id, model=model, embed_fn=embed_fn)
        totals["transcripts"] += 1
        for key in ("chunks", "embedded", "unchanged", "deleted"):
            totals[key] += stats.get(key, 0)
    return totals


def search_archive(
    query: str,
    *,
    workspace_key: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 8,
    query_vec: Optional[list[float]] = None,
    model: Optional[str] = None,
) -> list[ArchiveHit]:
    """Locate conversations. Returns pointers; does not load transcript bodies."""
    query = (query or "").strip()
    if not query:
        return []

    clauses: list[str] = []
    params: list[Any] = []
    if workspace_key:
        clauses.append("workspace_key = ?")
        params.append(workspace_key)
    if since:
        clauses.append("captured_at >= ?")
        params.append(since)
    if model:
        clauses.append("embedding_model = ?")
        params.append(model)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    # Only pay for vectors when something will rank with them. Selecting the
    # embedding column unconditionally made a lexical-only query transfer and
    # parse every vector in the workspace: measured at 12.8 s of a 13 s search,
    # against 115 ms of actual ranking.
    #
    # This never names the legacy JSON column, so it keeps working after 009
    # drops it. Rows predating the compact format are filled in afterwards.
    vector_columns = ", embedding_f32, embedding_dim" if query_vec else ""

    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT chunk_id, transcript_id, workspace_key, source_host, source_ref,
                   granularity, locale, seq_start, seq_end, captured_at, content{vector_columns}
            FROM session_transcript_chunks
            {where}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "id": row["chunk_id"],
            "transcript_id": row["transcript_id"],
            "workspace_key": row["workspace_key"],
            "source_host": row["source_host"],
            "source_ref": row["source_ref"],
            "granularity": row["granularity"],
            "locale": row.get("locale") or "original",
            "seq_start": int(row["seq_start"]),
            "seq_end": int(row["seq_end"]),
            "captured_at": _iso(row["captured_at"]),
            "content": row["content"] or "",
        }
        if query_vec:
            item["embedding"] = decode_vector(
                row["embedding_f32"],
                int(row["embedding_dim"]) if row["embedding_dim"] else None,
            )
        items.append(item)

    if query_vec:
        _fill_legacy_embeddings(items)

    ranked = hybrid_rank(query, items, query_vec=query_vec, limit=limit)
    hits: list[ArchiveHit] = []
    for item in ranked:
        hits.append(
            ArchiveHit(
                chunk_id=item["id"],
                transcript_id=item["transcript_id"],
                source_host=item["source_host"],
                source_ref=item["source_ref"],
                workspace_key=item["workspace_key"],
                granularity=item["granularity"],
                locale=item.get("locale") or "original",
                seq_start=item["seq_start"],
                seq_end=item["seq_end"],
                captured_at=item.get("captured_at"),
                score=float(item["score"]),
                snippet=make_snippet(item.get("content") or "", query),
                search_mode=item.get("search_mode", "hybrid"),
                content=item.get("content") or "",
            )
        )
    return hits


def detect_host_switch(
    pack: Optional[ResumePack], current_host: Optional[str]
) -> Optional[dict[str, Any]]:
    """Report that this machine is not the only one that has been in here.

    Comparing machine names alone conflates two situations that call for
    opposite actions:

    ``handoff``
        The last checkpoint came from another machine and nothing has been
        active since. One machine stopped, this one picks the thread up. This
        is what the fork machinery was built for, and forking is right.

    ``contention``
        Another machine has been active inside the liveness window. Forking
        here parks a session somebody is still using; the way forward is a
        separate lane.

    Reading machine names here does not break the write-only-provenance
    invariant: session lookup is unchanged and none of this reaches a lookup
    predicate. It only ever advises.
    """
    if pack is None or not current_host:
        return None

    others = [h for h in (pack.live_hosts or []) if h.get("host") != current_host]
    cp = pack.checkpoint or {}
    recorded = cp.get("host_hint")

    if others:
        kind = "contention"
    elif recorded and recorded != current_host:
        kind = "handoff"
    else:
        return None

    return {
        "kind": kind,
        "session_id": pack.session.session_id,
        "lane": pack.session.lane,
        "checkpoint_id": cp.get("checkpoint_id"),
        "checkpoint_host": recorded,
        "checkpoint_ide": cp.get("ide_hint"),
        "current_host": current_host,
        "live_hosts": others,
    }


def _fork_command(switch: dict[str, Any]) -> str:
    fork = f"agentloom-session open --fork-from {switch['session_id']}"
    if switch.get("checkpoint_id"):
        fork += f" --checkpoint {switch['checkpoint_id']}"
    return fork + " --reason host_switch"


def _render_handoff_banner(switch: dict[str, Any]) -> list[str]:
    origin = " / ".join(
        x for x in (switch.get("checkpoint_host"), switch.get("checkpoint_ide")) if x
    )
    return [
        "!!! HOST SWITCH — ask the operator before continuing !!!",
        f"  last checkpoint recorded on: {origin}",
        f"  this machine:                {switch['current_host']}",
        "",
        "  Ask which they want, then do exactly one:",
        f"    continue the same thread -> nothing to run; keep session {switch['session_id'][:8]}..",
        f"    branch for this machine  -> {_fork_command(switch)}",
        "",
        "  Do not choose for them.",
        "",
    ]


def _render_contention_banner(switch: dict[str, Any]) -> list[str]:
    lines = [
        "!!! ANOTHER MACHINE IS WORKING HERE — ask the operator before continuing !!!",
        f"  lane:         {switch.get('lane') or DEFAULT_LANE}",
    ]
    for host in switch.get("live_hosts") or []:
        where = " / ".join(x for x in (host.get("host"), host.get("ide")) if x)
        lines.append(f"  also active:  {where} (last seen {host.get('last_seen_at')})")
    lines += [
        f"  this machine: {switch['current_host']}",
        "",
        "  Forking would park their session out from under them. Do not do it.",
        "",
        "  Ask which they want, then do exactly one:",
        "    different work -> agentloom-session open --lane <name> --title \"...\"",
        "                      then pass --lane <name> to every later command here",
        f"    take over      -> {_fork_command(switch)} --force",
        "",
        "  Do not choose for them.",
        "",
    ]
    return lines


def render_host_switch_banner(switch: Optional[dict[str, Any]]) -> list[str]:
    """Render the stop-and-ask block for whichever situation was detected."""
    if not switch:
        return []
    if switch.get("kind") == "contention":
        return _render_contention_banner(switch)
    return _render_handoff_banner(switch)


def render_resume_pack(
    pack: Optional[ResumePack], current_host: Optional[str] = None
) -> str:
    """Render a resume pack as plain text.

    Output is deliberately format-neutral so any agent, in any host, can read it
    straight out of a terminal without parsing a proprietary structure.

    ``current_host`` enables the host-switch notice. It is omitted by callers
    that only want the raw pack.
    """
    if pack is None:
        return "No previous session found for this identity. Starting fresh."

    session = pack.session
    lines = [
        *render_host_switch_banner(detect_host_switch(pack, current_host)),
        "=== AgentLoom session resume ===",
        f"agent:     {session.agent_id}",
        f"operator:  {session.operator_id}",
        f"workspace: {session.workspace_key}",
        f"lane:      {session.lane}",
        f"session:   {session.session_id} ({session.status})",
    ]
    if session.title:
        lines.append(f"title:     {session.title}")
    lines.append(f"updated:   {session.updated_at}")
    for host in pack.live_hosts:
        where = " / ".join(x for x in (host.get("host"), host.get("ide")) if x)
        lines.append(f"also here: {where} (last seen {host.get('last_seen_at')})")

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
