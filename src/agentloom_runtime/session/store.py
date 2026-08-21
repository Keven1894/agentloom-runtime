"""Layer 0 working-session memory.

Read/write operations for cross-host session continuity. All lookups key on
``(agent_id, operator_id, workspace_key)``; nothing here reads editor-local
storage, and no query filters on a machine name, filesystem path, or IDE.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from agentloom_runtime.db import connect
from agentloom_runtime.session.identity import HostContext
from agentloom_runtime.session.index import (
    ArchiveHit,
    chunk_document,
    hybrid_rank,
    snippet as make_snippet,
)
from agentloom_runtime.session.transcript import TranscriptDocument

__all__ = [
    "ArchiveHit",
    "ResumePack",
    "SessionRecord",
    "TranscriptRecord",
    "add_turn",
    "checkpoint",
    "close_session",
    "get_session_lineage",
    "get_workspace_session_tree",
    "index_transcript",
    "index_workspace",
    "list_checkpoints",
    "list_transcripts",
    "load_transcript",
    "open_session",
    "park_session",
    "render_resume_pack",
    "resume",
    "search_archive",
    "search_sessions",
    "store_transcript",
]

CHECKPOINT_SCHEMA_VERSION = 1

_SESSION_COLUMNS = (
    "session_id, agent_id, operator_id, workspace_key, parent_session_id, "
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
            parent_session_id=row.get("parent_session_id") if isinstance(row, dict) else (row["parent_session_id"] if "parent_session_id" in row else None),
            fork_checkpoint_id=row.get("fork_checkpoint_id") if isinstance(row, dict) else (row["fork_checkpoint_id"] if "fork_checkpoint_id" in row else None),
            fork_reason=row.get("fork_reason") if isinstance(row, dict) else (row["fork_reason"] if "fork_reason" in row else None),
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
    parent_session_id: Optional[str] = None,
    fork_checkpoint_id: Optional[str] = None,
    fork_reason: Optional[str] = None,
) -> tuple[SessionRecord, bool]:
    """Return the open session for this identity, creating it if absent.

    Returns ``(session, created)``. At most one open session can exist per
    identity; the database enforces this with a unique generated key, so a
    concurrent opener loses the insert and reads the winner's row instead.
    """
    conn = connect()
    try:
        existing = _find_open(conn, agent_id, operator_id, workspace_key)
        if existing and parent_session_id:
            conn.execute(
                "UPDATE agent_sessions SET status = 'parked' WHERE session_id = ?",
                [existing["session_id"]],
            )
            conn.commit()
            existing = None

        if existing:
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
                    (session_id, agent_id, operator_id, workspace_key,
                     parent_session_id, fork_checkpoint_id, fork_reason,
                     status, title, workspace_path_hint, host_hint, ide_hint)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                [
                    session_id,
                    agent_id,
                    operator_id,
                    workspace_key,
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
    "operator_id, captured_at, turn_count, redaction_count, body_bytes, content_sha256"
)


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
) -> tuple[str, bool]:
    """Archive a normalized transcript. Returns ``(transcript_id, changed)``.

    Conversations grow while they happen, so re-archiving is expected and
    updates the existing row for that ``(source_host, source_ref)``. An
    unchanged body is detected by hash and skips the write entirely, which
    keeps repeated archiving cheap enough to run on every checkpoint.
    """
    body = json.dumps(doc.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    compressed = zlib.compress(body, 6)

    conn = connect()
    try:
        existing = conn.execute(
            "SELECT transcript_id, content_sha256 FROM session_transcripts "
            "WHERE source_host = ? AND source_ref = ?",
            [doc.source_host, doc.source_ref],
        ).fetchone()

        if existing:
            transcript_id = existing["transcript_id"]
            if existing["content_sha256"] == digest:
                return transcript_id, False
            conn.execute(
                """
                UPDATE session_transcripts
                SET session_id = COALESCE(?, session_id),
                    workspace_key = ?, agent_id = ?, operator_id = ?,
                    turn_count = ?, redaction_count = ?, body_bytes = ?,
                    content_sha256 = ?, body_zlib = ?
                WHERE transcript_id = ?
                """,
                [
                    session_id,
                    workspace_key,
                    agent_id,
                    operator_id,
                    doc.turn_count,
                    doc.redaction_count,
                    len(body),
                    digest,
                    compressed,
                    transcript_id,
                ],
            )
        else:
            transcript_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO session_transcripts
                    (transcript_id, session_id, source_host, source_ref, workspace_key,
                     agent_id, operator_id, turn_count, redaction_count, body_bytes,
                     content_sha256, body_zlib)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    transcript_id,
                    session_id,
                    doc.source_host,
                    doc.source_ref,
                    workspace_key,
                    agent_id,
                    operator_id,
                    doc.turn_count,
                    doc.redaction_count,
                    len(body),
                    digest,
                    compressed,
                ],
            )
        conn.commit()
        return transcript_id, True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_transcripts(
    workspace_key: Optional[str] = None,
    session_id: Optional[str] = None,
    source_ref: Optional[str] = None,
    limit: int = 20,
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

    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT {_TRANSCRIPT_COLUMNS} FROM session_transcripts {where} "
            "ORDER BY captured_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [TranscriptRecord.from_row(row) for row in rows]
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
    """
    row = _load_transcript_row(transcript_id=transcript_id)
    if row is None or row.get("body_zlib") is None:
        return {"chunks": 0, "embedded": 0, "unchanged": 0}

    doc = TranscriptDocument.from_dict(
        json.loads(zlib.decompress(row["body_zlib"]).decode("utf-8"))
    )
    chunks = chunk_document(doc)
    captured_at = row["captured_at"]
    workspace_key = row["workspace_key"]
    source_host = row["source_host"]
    source_ref = row["source_ref"]

    conn = connect()
    try:
        existing_rows = conn.execute(
            "SELECT chunk_id, granularity, seq_start, seq_end, content_sha256, embedding "
            "FROM session_transcript_chunks "
            "WHERE transcript_id = ? AND embedding_model = ?",
            [transcript_id, model],
        ).fetchall()
        existing = {
            (r["granularity"], int(r["seq_start"]), int(r["seq_end"])): r
            for r in existing_rows
        }

        wanted_keys = {(c.granularity, c.seq_start, c.seq_end) for c in chunks}
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
            key = (chunk.granularity, chunk.seq_start, chunk.seq_end)
            prev = existing.get(key)
            if prev and prev["content_sha256"] == chunk.content_sha256:
                has_vec = prev["embedding"] is not None
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
                     granularity, seq_start, seq_end, captured_at, content,
                     content_sha256, embedding, embedding_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON DUPLICATE KEY UPDATE
                    workspace_key = VALUES(workspace_key),
                    source_host = VALUES(source_host),
                    source_ref = VALUES(source_ref),
                    captured_at = VALUES(captured_at),
                    content = VALUES(content),
                    content_sha256 = VALUES(content_sha256),
                    embedding = NULL
                """,
                [
                    chunk_id,
                    transcript_id,
                    workspace_key,
                    source_host,
                    source_ref,
                    chunk.granularity,
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
                    "UPDATE session_transcript_chunks SET embedding = ? WHERE chunk_id = ?",
                    [json.dumps(vector), chunk_id],
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

    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT chunk_id, transcript_id, workspace_key, source_host, source_ref,
                   granularity, seq_start, seq_end, captured_at, content, embedding
            FROM session_transcript_chunks
            {where}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "id": row["chunk_id"],
                "transcript_id": row["transcript_id"],
                "workspace_key": row["workspace_key"],
                "source_host": row["source_host"],
                "source_ref": row["source_ref"],
                "granularity": row["granularity"],
                "seq_start": int(row["seq_start"]),
                "seq_end": int(row["seq_end"]),
                "captured_at": _iso(row["captured_at"]),
                "content": row["content"] or "",
                "embedding": _parse_embedding(row["embedding"]),
            }
        )

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
