"""Session memory: identity normalization, redaction, rendering, and the
host-neutrality invariants that keep this layer IDE-independent.

These tests require no database. Database-backed behavior is exercised
separately against a dedicated test database.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from agentloom_runtime.session import store
from agentloom_runtime.session.identity import (
    HostContext,
    normalize_workspace_key,
    resolve_lane,
)
from agentloom_runtime.session.store import ResumePack, SessionRecord, render_resume_pack
from agentloom_runtime.session.vcs import _summarize_status

SESSION_PKG = Path(__file__).resolve().parents[1] / "src" / "agentloom_runtime" / "session"
MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "mysql" / "004_session_memory.sql"
)

HINT_COLUMNS = ("host_hint", "ide_hint", "workspace_path_hint")


# --------------------------------------------------------------------------
# workspace identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:Acme/widget.git",
        "https://github.com/Acme/widget",
        "https://github.com/Acme/widget.git",
        "ssh://git@github.com:22/Acme/widget.git",
        "https://token@github.com/Acme/widget.git",
        "GIT@GITHUB.COM:acme/Widget.git",
    ],
)
def test_transports_collapse_to_one_workspace_key(url):
    assert normalize_workspace_key(url) == "github.com/acme/widget"


def test_self_hosted_remote_is_supported():
    assert (
        normalize_workspace_key("https://gitea.example.org/team/repo.git")
        == "gitea.example.org/team/repo"
    )


def test_distinct_repositories_do_not_collide():
    assert normalize_workspace_key("git@github.com:acme/a.git") != normalize_workspace_key(
        "git@github.com:acme/b.git"
    )


def test_empty_remote_is_rejected():
    with pytest.raises(ValueError):
        normalize_workspace_key("  ")


# --------------------------------------------------------------------------
# host neutrality — the property that makes this work in any IDE
# --------------------------------------------------------------------------


def _sources() -> dict[str, str]:
    """Every module in the session package, including host readers.

    Readers are the one place that touches host-specific storage, so they are
    exactly where a host-neutrality violation would show up first.
    """
    return {
        str(p.relative_to(SESSION_PKG)): p.read_text(encoding="utf-8")
        for p in SESSION_PKG.rglob("*.py")
    }


def test_hints_are_never_used_as_lookup_predicates():
    """Provenance hints may be written and displayed, never filtered on.

    If a hint ever reaches a WHERE clause, resuming from a second machine
    silently stops working, because that machine has different hints.
    """
    store_sql = _sources()["store.py"]
    for column in HINT_COLUMNS:
        assert not re.search(rf"\b{column}\s*=\s*\?", store_sql), (
            f"{column} is used as a query predicate; session lookup must key only on "
            "(agent_id, operator_id, workspace_key)"
        )


def test_session_layer_never_touches_a_hosts_private_chat_store():
    """Reading a host's plain transcript file is fine; opening its internal
    chat database is not. The latter is keyed by absolute path, changes shape
    between releases, and is held open by the running editor."""
    forbidden = ("state.vscdb", "workspaceStorage", "globalStorage", "composerData")
    for name, source in _sources().items():
        for token in forbidden:
            assert token not in source, f"{name} references a private chat store: {token}"


def test_session_layer_carries_no_deployment_identity():
    """The public runtime stays framework-only: no institution or instance names.

    Agent and operator names belong to a deployment, not to the framework. A
    hardcoded default is worse than a missing one: it does not fail, it quietly
    reads some other identity's session.
    """
    forbidden = re.compile(
        r"fiu\.edu|envita_prod|10\.100\.118\.\d+|envita-\w+|\bbguan\b",
        re.IGNORECASE,
    )
    for name, source in _sources().items():
        match = forbidden.search(source)
        assert not match, f"{name} leaks deployment-specific identity: {match.group(0)!r}"


def test_lexical_search_never_selects_the_embedding_column():
    """The single change that took search from 16 s to under a second.

    ``search_archive`` selected the embedding column unconditionally, so a
    lexical-only query transferred and parsed every vector in the workspace:
    measured at 12.8 s of a 13 s search, against 115 ms of actual ranking.
    Nothing about the result changed, which is why it went unnoticed.
    """
    captured: list[str] = []

    class _Conn:
        def execute(self, sql, params=None):
            captured.append(sql)
            return self

        def fetchall(self):
            return []

        def close(self):
            pass

    with patch("agentloom_runtime.session.store.connect", return_value=_Conn()):
        store.search_archive("anything", workspace_key="github.com/acme/repo")
    assert captured, "expected a query to be issued"
    assert "embedding" not in captured[0], (
        "lexical-only search must not pay for vectors it will not rank with"
    )

    captured.clear()
    with patch("agentloom_runtime.session.store.connect", return_value=_Conn()):
        store.search_archive(
            "anything", workspace_key="github.com/acme/repo", query_vec=[0.1, 0.2]
        )
    assert "embedding_f32" in captured[0], "vector search must request the vectors"


def test_reindex_tests_for_a_vector_rather_than_selecting_it():
    """Re-indexing only needs to know whether a vector exists.

    Selecting the column to check it for NULL would move the whole archive's
    embeddings on a run that changes nothing.
    """
    source = (SESSION_PKG / "store.py").read_text(encoding="utf-8")
    reindex_query = re.search(
        r"SELECT chunk_id, locale, granularity.*?FROM session_transcript_chunks",
        source,
        re.DOTALL,
    )
    assert reindex_query, "could not locate the re-index lookup"
    selected = reindex_query.group(0)
    assert "IS NOT NULL) AS has_vector" in selected, "must test for a vector"
    assert not re.search(r"\bembedding(_f32)?\s*,", selected), (
        "re-index must not select an embedding column, only test for one"
    )


def test_write_paths_do_not_name_the_column_009_dropped():
    """Only reads may mention the legacy JSON embedding column, and only guarded.

    Migration 009 drops ``embedding``. A write that still names it fails against
    a migrated database, and it would fail at index time — long after the change
    that introduced it looked fine.
    """
    source = (SESSION_PKG / "store.py").read_text(encoding="utf-8")
    for statement in re.findall(r"(?:INSERT INTO|UPDATE)\s+session_transcript_chunks.*?\"\"\"",
                                source, re.DOTALL):
        assert not re.search(r"\bembedding\b\s*(?:,|=)", statement), (
            "a write statement still names the dropped `embedding` column:\n"
            + statement[:300]
        )

    # The one read that may touch it must first check that it exists.
    legacy_reader = source[source.index("def _fill_legacy_embeddings"):]
    legacy_reader = legacy_reader[: legacy_reader.index("\ndef ", 1)]
    assert "_has_legacy_embedding_column()" in legacy_reader


def test_schema_keeps_hint_columns_out_of_lookup_indexes():
    ddl = MIGRATION.read_text(encoding="utf-8")
    index_lines = [
        line
        for line in ddl.splitlines()
        if re.search(r"\b(KEY|INDEX)\b", line) and "FOREIGN KEY" not in line
    ]
    assert index_lines, "expected index declarations in the migration"
    for line in index_lines:
        for column in HINT_COLUMNS:
            assert column not in line, f"hint column {column} indexed for lookup: {line.strip()}"


def test_schema_enforces_single_open_session_per_identity():
    ddl = MIGRATION.read_text(encoding="utf-8")
    assert "uq_agent_sessions_open" in ddl
    assert "open_key" in ddl


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


def test_sensitive_paths_are_withheld_from_checkpoints():
    porcelain = "\n".join(
        [
            " M src/app.py",
            " M .env",
            "?? secrets/token.json",
            " M deploy/server.pem",
            "?? .ssh/id_rsa",
            " M docs/plan/todo/thing.md",
        ]
    )
    summary = _summarize_status(porcelain)
    assert "src/app.py" in summary
    assert "docs/plan/todo/thing.md" in summary
    assert ".env" not in summary
    assert "token.json" not in summary
    assert "server.pem" not in summary
    assert "id_rsa" not in summary
    assert "4 sensitive path(s) withheld" in summary


def test_renamed_destination_is_checked_for_sensitivity():
    assert ".env" not in _summarize_status(' R  config.sample -> .env')


def test_long_status_is_truncated():
    porcelain = "\n".join(f" M file_{i}.py" for i in range(120))
    summary = _summarize_status(porcelain)
    assert "more changed path(s)" in summary
    assert len(summary.splitlines()) < 60


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _pack() -> ResumePack:
    return ResumePack(
        session=SessionRecord(
            session_id="s-1",
            agent_id="demo-builder",
            operator_id="alice",
            workspace_key="github.com/acme/widget",
            status="open",
            title="Session memory",
            updated_at="2026-08-14T00:00:00",
        ),
        checkpoint={
            "created_at": "2026-08-14T00:05:00",
            "host_hint": "laptop-a",
            "ide_hint": "cursor",
            "vcs_head": "abcdef1234567890",
            "vcs_branch": "main",
            "vcs_status_summary": " M src/app.py",
            "open_plan_path": "docs/plan/todo/thing.md",
            "next_action": "Apply the migration to the dev database.",
            "decisions": ["Store sessions in their own database"],
            "transcript_citations": ["uuid-1"],
            "payload": None,
        },
        turns=[{"seq": 1, "role": "human", "summary": "asked for a plan", "created_at": None}],
    )


def test_resume_pack_renders_next_action_as_plain_text():
    text = render_resume_pack(_pack())
    assert "NEXT ACTION:" in text
    assert "Apply the migration to the dev database." in text
    assert "github.com/acme/widget" in text
    assert "docs/plan/todo/thing.md" in text


def test_missing_session_renders_a_usable_message():
    assert "Starting fresh" in render_resume_pack(None)


def test_resume_on_another_machine_stops_and_asks():
    text = render_resume_pack(_pack(), current_host="laptop-b")
    assert "HOST SWITCH" in text
    assert "laptop-a" in text and "laptop-b" in text
    assert "--reason host_switch" in text
    assert "Do not choose for them." in text
    # The banner must precede the pack, or an agent skimming the head misses it.
    assert text.index("HOST SWITCH") < text.index("NEXT ACTION:")


def test_resume_on_the_same_machine_is_unchanged():
    assert "HOST SWITCH" not in render_resume_pack(_pack(), current_host="laptop-a")


def test_resume_without_a_current_host_does_not_guess():
    assert "HOST SWITCH" not in render_resume_pack(_pack())


def test_host_switch_detection_needs_a_checkpoint():
    pack = _pack()
    pack.checkpoint = None
    assert store.detect_host_switch(pack, "laptop-b") is None
    assert store.detect_host_switch(None, "laptop-b") is None


def test_host_switch_reports_the_fork_target():
    switch = store.detect_host_switch(_pack(), "laptop-b")
    assert switch["session_id"] == "s-1"
    assert switch["checkpoint_host"] == "laptop-a"
    assert switch["current_host"] == "laptop-b"


def test_host_context_is_only_provenance():
    host = HostContext(host_hint="h", ide_hint="i", workspace_path_hint="p")
    assert (host.host_hint, host.ide_hint, host.workspace_path_hint) == ("h", "i", "p")


def test_session_record_supports_dag_lineage_fields():
    rec = SessionRecord(
        session_id="s-child",
        agent_id="test-agent",
        operator_id="test-op",
        workspace_key="github.com/acme/widget",
        status="open",
        parent_session_id="s-parent",
        fork_checkpoint_id="cp-123",
        fork_reason="host_switch",
        title="Child session",
    )
    d = rec.to_dict()
    assert d["parent_session_id"] == "s-parent"
    assert d["fork_checkpoint_id"] == "cp-123"
    assert d["fork_reason"] == "host_switch"
    from_d = SessionRecord.from_row(d)
    assert from_d.parent_session_id == "s-parent"
    assert from_d.fork_checkpoint_id == "cp-123"
    assert from_d.fork_reason == "host_switch"


# --------------------------------------------------------------------------
# lanes — concurrent work streams in one repository
# --------------------------------------------------------------------------


def test_lane_defaults_to_the_shared_lane(monkeypatch):
    """Every session that predates lanes lives in 'default'.

    A host that never passes a lane has to keep resuming exactly what it
    resumed before, or this becomes a breaking change for every deployment.
    """
    monkeypatch.delenv("AGENTLOOM_SESSION_LANE", raising=False)
    assert resolve_lane() == "default"
    assert resolve_lane("  ") == "default"


def test_lane_can_be_pinned_per_checkout(monkeypatch):
    monkeypatch.setenv("AGENTLOOM_SESSION_LANE", "medialoom")
    assert resolve_lane() == "medialoom"
    assert resolve_lane("explicit") == "explicit", "an explicit lane outranks the environment"


def test_open_session_lookup_keys_only_on_identity_and_lane():
    """The positive form of the host-neutrality invariant.

    The name-based prohibition elsewhere catches a hint reaching a predicate.
    This catches the other direction: that the lookup keys on the whole
    identity and nothing else, so a second machine resolves the same session.
    """
    import inspect

    source = inspect.getsource(store._find_open)
    where = source[source.index("WHERE") : source.index("LIMIT")]
    predicates = set(re.findall(r"(\w+)\s*=\s*[?']", where))
    assert predicates == {"agent_id", "operator_id", "workspace_key", "lane", "status"}


def test_session_record_defaults_to_the_shared_lane():
    rec = SessionRecord(
        session_id="s-1",
        agent_id="a",
        operator_id="o",
        workspace_key="github.com/acme/widget",
        status="open",
    )
    assert rec.lane == "default"
    assert SessionRecord.from_row(rec.to_dict()).lane == "default"


def test_session_record_reads_a_row_written_before_lanes_existed():
    """016 backfills every row, but hand-built rows and older callers do not."""
    row = {
        "session_id": "s-1",
        "agent_id": "a",
        "operator_id": "o",
        "workspace_key": "github.com/acme/widget",
        "status": "open",
        "title": None,
        "workspace_path_hint": None,
        "host_hint": None,
        "ide_hint": None,
        "created_at": None,
        "updated_at": None,
        "last_checkpoint_at": None,
    }
    assert SessionRecord.from_row(row).lane == "default"


def test_resume_pack_shows_the_lane():
    assert "lane:      default" in render_resume_pack(_pack())


# --------------------------------------------------------------------------
# concurrent hosts — the case a hostname comparison alone cannot see
# --------------------------------------------------------------------------


def _busy_pack() -> ResumePack:
    pack = _pack()
    pack.live_hosts = [
        {
            "host": "laptop-a",
            "ide": "cursor",
            "first_seen_at": "2026-08-14T00:00:00",
            "last_seen_at": "2026-08-14T00:31:00",
        }
    ]
    return pack


def test_a_live_second_machine_is_contention_not_a_handoff():
    """The distinction the whole guard rests on.

    Same two hostnames either way. Only recent activity separates "they left,
    take over" from "they are typing, do not park them".
    """
    assert store.detect_host_switch(_pack(), "laptop-b")["kind"] == "handoff"
    assert store.detect_host_switch(_busy_pack(), "laptop-b")["kind"] == "contention"


def test_contention_banner_refuses_the_fork_and_offers_a_lane():
    text = render_resume_pack(_busy_pack(), current_host="laptop-b")
    assert "ANOTHER MACHINE IS WORKING HERE" in text
    assert "HOST SWITCH" not in text, "the handoff advice would park a live session"
    assert "--lane" in text
    assert "Do not do it." in text
    assert "--force" in text, "taking over must stay possible, just never silent"
    assert text.index("ANOTHER MACHINE") < text.index("NEXT ACTION:")


def test_our_own_heartbeat_is_not_company():
    """A host that only ever sees itself in session_hosts must not be warned."""
    pack = _pack()
    pack.live_hosts = [{"host": "laptop-a", "ide": "cursor", "last_seen_at": "x"}]
    assert store.detect_host_switch(pack, "laptop-a") is None


def test_contention_is_reported_even_without_a_checkpoint():
    """Liveness does not depend on anyone having checkpointed yet.

    A machine that opened a session an hour ago and has not checkpointed is
    still working there; requiring a checkpoint would hide exactly that case.
    """
    pack = _busy_pack()
    pack.checkpoint = None
    switch = store.detect_host_switch(pack, "laptop-b")
    assert switch["kind"] == "contention"
    assert switch["live_hosts"][0]["host"] == "laptop-a"


def test_live_window_is_generous_and_overridable(monkeypatch):
    """Erring toward "still live" costs a spare lane; erring the other way
    parks somebody's session. The default reflects that asymmetry."""
    monkeypatch.delenv("AGENTLOOM_SESSION_LIVE_MINUTES", raising=False)
    assert store._live_window_minutes() >= 60
    monkeypatch.setenv("AGENTLOOM_SESSION_LIVE_MINUTES", "15")
    assert store._live_window_minutes() == 15
    monkeypatch.setenv("AGENTLOOM_SESSION_LIVE_MINUTES", "not-a-number")
    assert store._live_window_minutes() == store.DEFAULT_LIVE_WINDOW_MINUTES


def test_the_fork_guard_does_not_depend_on_the_other_host_being_upgraded():
    """The guard's floor is data every release writes.

    `session_hosts` only fills up once a host runs lane-aware code, so during a
    rollout the heartbeat is empty for exactly the machine most at risk of
    being parked. The guard has to fall back to the session row, which every
    version of the client updates.
    """
    import inspect

    source = inspect.getsource(store.open_session)
    guard = source[source.index("if not force:") : source.index("raise SessionInUseError")]
    assert "_live_hosts" in guard
    assert "_implied_activity" in guard, "no fallback: an un-upgraded host is invisible"

    implied = inspect.getsource(store._implied_activity)
    assert "host_hint" in implied and "last_checkpoint_at" in implied
    assert "agent_sessions" in implied


def test_session_in_use_error_names_the_machine_to_ask_about():
    exc = store.SessionInUseError(
        "s-1", [{"host": "fiu-gis-center", "last_seen_at": "2026-09-02T19:29:09"}]
    )
    assert "fiu-gis-center" in str(exc)
    assert "s-1" in str(exc)
    assert exc.hosts[0]["host"] == "fiu-gis-center"


def test_cli_render_tree_node():
    from agentloom_runtime.session.cli import _render_tree_node

    root = {
        "session_id": "11111111-0000-0000-0000-000000000000",
        "agent_id": "envita-builder",
        "operator_id": "alice",
        "status": "parked",
        "title": "Root task",
        "children": [
            {
                "session_id": "22222222-0000-0000-0000-000000000000",
                "agent_id": "envita-builder",
                "operator_id": "alice",
                "status": "open",
                "title": "Child continuation",
                "fork_reason": "host_switch",
                "children": [],
            }
        ],
    }
    lines = _render_tree_node(root)
    assert len(lines) == 2
    assert "11111111" in lines[0]
    assert "Root task" in lines[0]
    assert "22222222" in lines[1]
    assert "forked: host_switch" in lines[1]

