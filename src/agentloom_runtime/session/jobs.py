"""Durable trace for long-running jobs over the Layer 0 archive.

A batch that translates, re-embeds, or re-summarizes hundreds of transcripts
runs for hours and gets interrupted. Where it got to, what it decided, and why
all have to outlive the machine it started on, or the work is the property of
one laptop.

Progress is the cheap part and is mostly derivable from the archive itself.
The expensive part is judgement: a reviewer model's score, its diagnosis, and
the patch it proposed cost money to produce and may not reproduce on a re-run.
That is what :func:`record_item` keeps.

Three verbs cover a job's lifetime: :func:`start_run` opens it, :func:`log_event`
appends to the trace, and :func:`record_item` writes per-transcript state that
the next run reads back through :func:`get_item` to decide what to skip.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from agentloom_runtime.db import connect
from agentloom_runtime.session.identity import detect_host_context

__all__ = [
    "JobItem",
    "JobRun",
    "finish_run",
    "get_item",
    "list_events",
    "list_items",
    "list_runs",
    "log_event",
    "record_item",
    "run_summary",
    "start_run",
]

_RUN_COLUMNS = (
    "run_id, job_kind, host, operator_id, workspace_key, args_json, status, "
    "started_at, finished_at, items_total, items_done, items_failed"
)

_ITEM_COLUMNS = (
    "job_kind, transcript_id, last_run_id, status, body_sha256, attempt, "
    "turns_total, qc_model, qc_score, qc_passed, qc_report_json, "
    "patches_applied, error_text, started_at, updated_at"
)

# Terminal states. A job asks "is this already done" far more often than it
# writes, so the answer lives next to the states rather than in each caller.
DONE_STATES = frozenset({"qc_passed", "completed"})


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
class JobRun:
    run_id: str
    job_kind: str
    status: str
    host: Optional[str] = None
    operator_id: Optional[str] = None
    workspace_key: Optional[str] = None
    args: dict[str, Any] = field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    items_total: int = 0
    items_done: int = 0
    items_failed: int = 0

    @classmethod
    def from_row(cls, row: Any) -> "JobRun":
        return cls(
            run_id=row["run_id"],
            job_kind=row["job_kind"],
            status=row["status"],
            host=row["host"],
            operator_id=row["operator_id"],
            workspace_key=row["workspace_key"],
            args=_from_json(row["args_json"]) or {},
            started_at=_iso(row["started_at"]),
            finished_at=_iso(row["finished_at"]),
            items_total=int(row["items_total"] or 0),
            items_done=int(row["items_done"] or 0),
            items_failed=int(row["items_failed"] or 0),
        )


@dataclass
class JobItem:
    job_kind: str
    transcript_id: str
    status: str
    last_run_id: Optional[str] = None
    body_sha256: Optional[str] = None
    attempt: int = 0
    turns_total: int = 0
    qc_model: Optional[str] = None
    qc_score: Optional[float] = None
    qc_passed: Optional[bool] = None
    qc_report: Optional[dict[str, Any]] = None
    patches_applied: int = 0
    error_text: Optional[str] = None
    started_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: Any) -> "JobItem":
        score = row["qc_score"]
        passed = row["qc_passed"]
        return cls(
            job_kind=row["job_kind"],
            transcript_id=row["transcript_id"],
            status=row["status"],
            last_run_id=row["last_run_id"],
            body_sha256=row["body_sha256"],
            attempt=int(row["attempt"] or 0),
            turns_total=int(row["turns_total"] or 0),
            qc_model=row["qc_model"],
            qc_score=float(score) if score is not None else None,
            qc_passed=bool(passed) if passed is not None else None,
            qc_report=_from_json(row["qc_report_json"]),
            patches_applied=int(row["patches_applied"] or 0),
            error_text=row["error_text"],
            started_at=_iso(row["started_at"]),
            updated_at=_iso(row["updated_at"]),
        )

    def is_fresh_for(self, body_sha256: Optional[str]) -> bool:
        """Whether this item may be skipped for an archive with this fingerprint.

        An edited transcript must re-enter the queue rather than inherit the
        verdict passed on its earlier text, so a fingerprint mismatch is not
        done no matter how good the old score was.
        """
        if self.status not in DONE_STATES:
            return False
        if body_sha256 is None or self.body_sha256 is None:
            return False
        return self.body_sha256 == body_sha256


def start_run(
    job_kind: str,
    *,
    args: Optional[dict[str, Any]] = None,
    workspace_key: Optional[str] = None,
    operator_id: Optional[str] = None,
    items_total: int = 0,
    conn: Any = None,
) -> JobRun:
    """Open a run and return it. The caller keeps ``run_id`` for the trace."""
    own = conn is None
    conn = conn or connect()
    try:
        run_id = str(uuid.uuid4())
        host = detect_host_context().host_hint
        conn.execute(
            "INSERT INTO session_job_runs "
            "(run_id, job_kind, host, operator_id, workspace_key, args_json, "
            " status, items_total) "
            "VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
            [run_id, job_kind, host, operator_id, workspace_key, _as_json(args), items_total],
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM session_job_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        return JobRun.from_row(row)
    finally:
        if own:
            conn.close()


def finish_run(
    run_id: str,
    *,
    status: str = "completed",
    items_done: Optional[int] = None,
    items_failed: Optional[int] = None,
    conn: Any = None,
) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        sets = ["status = ?", "finished_at = ?"]
        params: list[Any] = [status, datetime.now()]
        if items_done is not None:
            sets.append("items_done = ?")
            params.append(items_done)
        if items_failed is not None:
            sets.append("items_failed = ?")
            params.append(items_failed)
        params.append(run_id)
        conn.execute(
            f"UPDATE session_job_runs SET {', '.join(sets)} WHERE run_id = ?", params
        )
        conn.commit()
    finally:
        if own:
            conn.close()


# Sequence numbers are monotonic within a run. Jobs are single-writer, so the
# counter is cached per run rather than re-queried for every event.
_seq_cache: dict[str, int] = {}


def log_event(
    run_id: str,
    event_type: str,
    *,
    transcript_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    conn: Any = None,
) -> int:
    """Append one typed event and return its sequence number within the run."""
    own = conn is None
    conn = conn or connect()
    try:
        seq = _seq_cache.get(run_id)
        if seq is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS top FROM session_job_events WHERE run_id = ?",
                [run_id],
            ).fetchone()
            seq = int(row["top"] or 0)
        seq += 1
        conn.execute(
            "INSERT INTO session_job_events "
            "(run_id, seq, ts, event_type, transcript_id, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                run_id,
                seq,
                datetime.now(timezone.utc).astimezone().replace(tzinfo=None),
                event_type,
                transcript_id,
                _as_json(payload),
            ],
        )
        conn.commit()
        _seq_cache[run_id] = seq
        return seq
    finally:
        if own:
            conn.close()


def record_item(
    job_kind: str,
    transcript_id: str,
    *,
    status: str,
    run_id: Optional[str] = None,
    body_sha256: Optional[str] = None,
    turns_total: Optional[int] = None,
    qc_model: Optional[str] = None,
    qc_score: Optional[float] = None,
    qc_passed: Optional[bool] = None,
    qc_report: Optional[dict[str, Any]] = None,
    patches_applied: Optional[int] = None,
    error_text: Optional[str] = None,
    bump_attempt: bool = False,
    conn: Any = None,
) -> None:
    """Upsert per-transcript state.

    Omitted fields keep their stored value, so a mid-pipeline status write does
    not erase the verdict from an earlier stage of the same run.
    """
    own = conn is None
    conn = conn or connect()
    try:
        optional: dict[str, Any] = {
            "body_sha256": body_sha256,
            "turns_total": turns_total,
            "qc_model": qc_model,
            "qc_score": qc_score,
            "qc_passed": None if qc_passed is None else int(qc_passed),
            "qc_report_json": _as_json(qc_report),
            "patches_applied": patches_applied,
            "error_text": error_text,
        }
        provided = {k: v for k, v in optional.items() if v is not None}

        columns = ["job_kind", "transcript_id", "last_run_id", "status", "started_at", "attempt"]
        values: list[Any] = [job_kind, transcript_id, run_id, status, datetime.now(), 1 if bump_attempt else 0]
        columns.extend(provided.keys())
        values.extend(provided.values())

        updates = ["status = VALUES(status)", "last_run_id = VALUES(last_run_id)"]
        if bump_attempt:
            updates.append("attempt = attempt + 1")
        updates.extend(f"{col} = VALUES({col})" for col in provided)

        placeholders = ", ".join(["?"] * len(columns))
        conn.execute(
            f"INSERT INTO session_job_items ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {', '.join(updates)}",
            values,
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def get_item(job_kind: str, transcript_id: str, conn: Any = None) -> Optional[JobItem]:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM session_job_items "
            "WHERE job_kind = ? AND transcript_id = ?",
            [job_kind, transcript_id],
        ).fetchone()
        return JobItem.from_row(row) if row else None
    finally:
        if own:
            conn.close()


def list_items(
    job_kind: str,
    *,
    status: Optional[str] = None,
    limit: int = 500,
    conn: Any = None,
) -> list[JobItem]:
    own = conn is None
    conn = conn or connect()
    try:
        sql = f"SELECT {_ITEM_COLUMNS} FROM session_job_items WHERE job_kind = ?"
        params: list[Any] = [job_kind]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        return [JobItem.from_row(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        if own:
            conn.close()


def list_runs(
    job_kind: Optional[str] = None,
    *,
    limit: int = 20,
    conn: Any = None,
) -> list[JobRun]:
    own = conn is None
    conn = conn or connect()
    try:
        sql = f"SELECT {_RUN_COLUMNS} FROM session_job_runs"
        params: list[Any] = []
        if job_kind:
            sql += " WHERE job_kind = ?"
            params.append(job_kind)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(int(limit))
        return [JobRun.from_row(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        if own:
            conn.close()


def list_events(
    run_id: str,
    *,
    event_type: Optional[str] = None,
    limit: int = 500,
    conn: Any = None,
) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        sql = (
            "SELECT seq, ts, event_type, transcript_id, payload_json "
            "FROM session_job_events WHERE run_id = ?"
        )
        params: list[Any] = [run_id]
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY seq ASC LIMIT ?"
        params.append(int(limit))
        return [
            {
                "seq": int(r["seq"]),
                "ts": _iso(r["ts"]),
                "event_type": r["event_type"],
                "transcript_id": r["transcript_id"],
                "payload": _from_json(r["payload_json"]),
            }
            for r in conn.execute(sql, params).fetchall()
        ]
    finally:
        if own:
            conn.close()


def run_summary(job_kind: str, conn: Any = None) -> dict[str, Any]:
    """Counts by state plus the mean review score, for an audit view."""
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n, AVG(qc_score) AS mean_score "
            "FROM session_job_items WHERE job_kind = ? GROUP BY status",
            [job_kind],
        ).fetchall()
        by_status = {
            r["status"]: {
                "count": int(r["n"]),
                "mean_score": float(r["mean_score"]) if r["mean_score"] is not None else None,
            }
            for r in rows
        }
        return {
            "job_kind": job_kind,
            "total": sum(v["count"] for v in by_status.values()),
            "by_status": by_status,
        }
    finally:
        if own:
            conn.close()
