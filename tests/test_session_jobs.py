"""Durable job trace: resume semantics and the shape of what gets written.

No database. A fake connection records the statements so the invariants that
matter — partial updates never erase a verdict, an edited body never inherits
a pass, event sequence numbers stay monotonic — are checked directly.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from agentloom_runtime.session import jobs
from agentloom_runtime.session.jobs import JobItem


class FakeCursor:
    def __init__(self, row: Optional[dict] = None, rows: Optional[list[dict]] = None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConn:
    """Records every statement; replies with whatever the test queued."""

    def __init__(self, replies: Optional[list[Any]] = None):
        self.statements: list[tuple[str, Any]] = []
        self.commits = 0
        self._replies = list(replies or [])

    def execute(self, sql: str, params: Any = None):
        self.statements.append((" ".join(sql.split()), params))
        if self._replies:
            return self._replies.pop(0)
        return FakeCursor()

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _item(**overrides) -> JobItem:
    base = dict(
        job_kind="transcript_presentation",
        transcript_id="t-1",
        status="qc_passed",
        body_sha256="a" * 64,
    )
    base.update(overrides)
    return JobItem(**base)


# --------------------------------------------------------------------------
# resume semantics
# --------------------------------------------------------------------------


def test_matching_fingerprint_on_a_passed_item_is_skippable():
    assert _item().is_fresh_for("a" * 64) is True


def test_edited_body_reenters_the_queue_despite_an_earlier_pass():
    """A verdict belongs to the text it was granted on, not to the row."""
    assert _item().is_fresh_for("b" * 64) is False


def test_unfinished_item_is_never_skippable():
    assert _item(status="turns_done").is_fresh_for("a" * 64) is False


def test_missing_fingerprint_is_treated_as_unknown_rather_than_fresh():
    assert _item(body_sha256=None).is_fresh_for("a" * 64) is False
    assert _item().is_fresh_for(None) is False


# --------------------------------------------------------------------------
# record_item
# --------------------------------------------------------------------------


def test_status_only_write_does_not_clear_an_existing_verdict():
    """Mid-pipeline writes happen after the audit within the same run.

    Listing every column in the UPDATE would blank the score and the report
    that an earlier stage just stored.
    """
    conn = FakeConn()
    jobs.record_item("k", "t-1", status="indexed", run_id="r-1", conn=conn)

    sql, _ = conn.statements[0]
    assert "qc_report_json" not in sql
    assert "qc_score" not in sql
    assert "status = VALUES(status)" in sql


def test_verdict_write_carries_score_report_and_fingerprint():
    conn = FakeConn()
    jobs.record_item(
        "k",
        "t-1",
        status="qc_passed",
        run_id="r-1",
        body_sha256="c" * 64,
        qc_model="gpt-5.5",
        qc_score=0.93,
        qc_passed=True,
        qc_report={"summary": "fine", "flagged_issues": []},
        patches_applied=2,
        conn=conn,
    )

    sql, params = conn.statements[0]
    for column in ("qc_model", "qc_score", "qc_passed", "qc_report_json", "body_sha256"):
        assert f"{column} = VALUES({column})" in sql
    assert 0.93 in params
    assert conn.commits == 1


def test_a_false_verdict_is_stored_rather_than_dropped_as_empty():
    """`qc_passed=False` and `qc_score=0.0` are findings, not missing values."""
    conn = FakeConn()
    jobs.record_item("k", "t-1", status="qc_failed", qc_passed=False, qc_score=0.0, conn=conn)

    sql, params = conn.statements[0]
    assert "qc_passed = VALUES(qc_passed)" in sql
    assert "qc_score = VALUES(qc_score)" in sql
    assert 0 in params


def test_retry_increments_the_attempt_counter_in_place():
    conn = FakeConn()
    jobs.record_item("k", "t-1", status="error", bump_attempt=True, conn=conn)

    sql, _ = conn.statements[0]
    assert "attempt = attempt + 1" in sql


# --------------------------------------------------------------------------
# event trace
# --------------------------------------------------------------------------


def test_first_event_of_a_run_continues_from_what_is_already_stored():
    """A resumed process must not restart the sequence and collide."""
    jobs._seq_cache.pop("r-9", None)
    conn = FakeConn(replies=[FakeCursor(row={"top": 7})])

    seq = jobs.log_event("r-9", "job/start", conn=conn)

    assert seq == 8
    insert_sql, params = conn.statements[1]
    assert "INSERT INTO session_job_events" in insert_sql
    assert 8 in params


def test_subsequent_events_advance_without_requerying_the_maximum():
    jobs._seq_cache.pop("r-10", None)
    conn = FakeConn(replies=[FakeCursor(row={"top": 0})])

    first = jobs.log_event("r-10", "llm/call", conn=conn)
    second = jobs.log_event("r-10", "qc/verdict", transcript_id="t-1", conn=conn)

    assert (first, second) == (1, 2)
    selects = [s for s, _ in conn.statements if s.startswith("SELECT")]
    assert len(selects) == 1


def test_event_payload_is_serialized_as_json_text():
    jobs._seq_cache.pop("r-11", None)
    conn = FakeConn(replies=[FakeCursor(row={"top": 0})])

    jobs.log_event("r-11", "qc/verdict", payload={"score": 0.9, "zh": "中文"}, conn=conn)

    _, params = conn.statements[1]
    blob = [p for p in params if isinstance(p, str) and p.startswith("{")][0]
    assert '"score": 0.9' in blob
    assert "中文" in blob  # not escaped into \u sequences


@pytest.mark.parametrize("state", sorted(jobs.DONE_STATES))
def test_terminal_states_are_the_only_skippable_ones(state):
    assert _item(status=state).is_fresh_for("a" * 64) is True
