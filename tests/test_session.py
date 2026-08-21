"""Session memory: identity normalization, redaction, rendering, and the
host-neutrality invariants that keep this layer IDE-independent.

These tests require no database. Database-backed behavior is exercised
separately against a dedicated test database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentloom_runtime.session.identity import HostContext, normalize_workspace_key
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

