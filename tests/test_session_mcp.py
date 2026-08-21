"""The Layer 0 session MCP server: protocol shape and store contract.

Every ``patch`` here binds ``autospec=True`` on purpose. A plain ``MagicMock``
accepts any argument, so a tool calling the store with a parameter the store
does not have still passes — the failure then surfaces only when a real host
calls the tool. Autospec moves that failure into the test.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from agentloom_runtime.session import mcp as mcp_mod
from agentloom_runtime.session import store as store_mod
from agentloom_runtime.session.index import ArchiveHit
from agentloom_runtime.session.mcp import TOOLS, TOOL_HANDLERS, handle_request
from agentloom_runtime.session.store import ResumePack, SessionRecord
from agentloom_runtime.session.transcript import TranscriptBlock, TranscriptDocument, TranscriptTurn


def test_mcp_initialize():
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    resp = handle_request(req)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"] == "agentloom-session"


def test_mcp_ping():
    req = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    resp = handle_request(req)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 2
    assert resp["result"] == {}


def test_mcp_tools_list():
    req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    resp = handle_request(req)
    assert resp["jsonrpc"] == "2.0"
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "session_search" in names
    assert "session_get_context" in names
    assert "session_get_checkpoint" in names
    assert "session_get_lineage" in names


def test_mcp_session_search_dispatch():
    mock_hit = ArchiveHit(
        chunk_id="c-1",
        transcript_id="t-1",
        workspace_key="github.com/acme/widget",
        source_host="cursor",
        source_ref="ref-abc",
        granularity="window",
        seq_start=10,
        seq_end=15,
        score=0.85,
        snippet="decided to use MySQL for session memory",
        search_mode="hybrid",
        captured_at="2026-08-18T12:00:00",
    )
    with patch(
        "agentloom_runtime.session.store.search_archive",
        autospec=True,
        return_value=[mock_hit],
    ), patch(
        "agentloom_runtime.memory.embedding_provider.embed_query",
        autospec=True,
        return_value=[0.1, 0.2, 0.3],
    ):
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "session_search",
                "arguments": {"query": "MySQL session memory", "workspace_key": "github.com/acme/widget"},
            },
        }
        resp = handle_request(req)
        assert not resp["result"]["isError"]
        text = resp["result"]["content"][0]["text"]
        assert "ref-abc" in text
        assert "decided to use MySQL" in text
        assert "session_get_context" in text


def test_mcp_session_get_context_dispatch():
    doc = TranscriptDocument(
        source_host="cursor",
        source_ref="ref-abc",
        turns=[
            TranscriptTurn(seq=1, role="human", blocks=[TranscriptBlock(type="text", text="Hello")]),
            TranscriptTurn(seq=2, role="agent", blocks=[TranscriptBlock(type="text", text="Hi there!")]),
        ],
    )
    with patch(
        "agentloom_runtime.session.store.load_transcript", autospec=True, return_value=doc
    ):
        req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "session_get_context",
                "arguments": {"source_ref": "ref-abc", "around_seq": 1, "radius": 2},
            },
        }
        resp = handle_request(req)
        assert not resp["result"]["isError"]
        text = resp["result"]["content"][0]["text"]
        assert "ref-abc" in text
        assert "Hi there!" in text


def test_mcp_session_get_checkpoint_dispatch():
    pack = ResumePack(
        session=SessionRecord(
            session_id="s-1",
            agent_id="envita-builder",
            operator_id="alice",
            workspace_key="github.com/acme/widget",
            status="open",
            title="Session DAG",
        ),
        checkpoint={
            "created_at": "2026-08-18T12:00:00",
            "next_action": "Implement Web UI",
            "open_plan_path": "docs/plan/todo/plan.md",
            "decisions": ["Keep host neutral"],
            "vcs_branch": "main",
            "vcs_head": "abcdef123456",
        },
    )
    with patch("agentloom_runtime.session.store.resume", autospec=True, return_value=pack):
        req = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "session_get_checkpoint",
                "arguments": {
                    "agent_id": "acme-builder",
                    "workspace_key": "github.com/acme/widget",
                },
            },
        }
        resp = handle_request(req)
        assert not resp["result"]["isError"]
        text = resp["result"]["content"][0]["text"]
        assert "NEXT ACTION:" in text
        assert "Implement Web UI" in text


def test_identity_tools_refuse_to_guess_an_agent(monkeypatch):
    """No default agent name.

    Inventing one does not fail loudly — it reads a different identity's
    session and returns a confident, wrong answer.
    """
    monkeypatch.delenv("AGENTLOOM_AGENT_ID", raising=False)

    for tool in ("session_get_checkpoint", "session_get_lineage"):
        text = mcp_mod.TOOL_HANDLERS[tool]({"workspace_key": "github.com/acme/widget"})
        assert "AGENTLOOM_AGENT_ID" in text, f"{tool} should explain how to supply an agent"


def test_lineage_falls_back_to_the_callers_own_session(monkeypatch):
    """An agent asking 'how did I get here' has no session id to pass."""
    monkeypatch.setenv("AGENTLOOM_AGENT_ID", "acme-builder")
    pack = ResumePack(
        session=SessionRecord(
            session_id="resolved-me",
            agent_id="acme-builder",
            operator_id="alice",
            workspace_key="github.com/acme/widget",
            status="open",
        ),
        checkpoint=None,
    )
    lineage = {
        "session": {
            "session_id": "resolved-me",
            "agent_id": "acme-builder",
            "operator_id": "alice",
            "workspace_key": "github.com/acme/widget",
            "status": "open",
            "title": None,
        },
        "ancestors": [],
        "children": [],
    }
    with patch(
        "agentloom_runtime.session.store.resume", autospec=True, return_value=pack
    ), patch(
        "agentloom_runtime.session.store.get_session_lineage",
        autospec=True,
        return_value=lineage,
    ) as get_lineage:
        text = mcp_mod.tool_session_get_lineage({"workspace_key": "github.com/acme/widget"})

    get_lineage.assert_called_once_with("resolved-me")
    assert "resolved-me" in text


def test_mcp_session_get_lineage_dispatch():
    lineage = {
        "session": {
            "session_id": "child-123",
            "agent_id": "envita-builder",
            "operator_id": "alice",
            "workspace_key": "github.com/acme/widget",
            "status": "open",
            "title": "Child session",
            "fork_reason": "host_switch",
            "fork_checkpoint_id": "cp-001",
        },
        "ancestors": [
            {
                "session_id": "parent-456",
                "agent_id": "envita-builder",
                "operator_id": "alice",
                "status": "parked",
                "title": "Parent session",
                "created_at": "2026-08-17T10:00:00",
            }
        ],
        "children": [],
    }
    with patch(
        "agentloom_runtime.session.store.get_session_lineage",
        autospec=True,
        return_value=lineage,
    ):
        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "session_get_lineage",
                "arguments": {"session_id": "child-123"},
            },
        }
        resp = handle_request(req)
        assert not resp["result"]["isError"]
        text = resp["result"]["content"][0]["text"]
        assert "child-123" in text
        assert "parent-456" in text
        assert "host_switch" in text


def test_mcp_unknown_tool():
    req = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {
            "name": "non_existent_tool",
            "arguments": {},
        },
    }
    resp = handle_request(req)
    assert resp["result"]["isError"]
    assert "unknown tool" in resp["result"]["content"][0]["text"]


def test_mcp_unknown_method():
    req = {"jsonrpc": "2.0", "id": 9, "method": "invalid/method", "params": {}}
    resp = handle_request(req)
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_notifications_get_no_response():
    """A JSON-RPC notification has no id and must not be answered."""
    assert handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


# --------------------------------------------------------------------------
# contract: what the server advertises must match what it can actually run
# --------------------------------------------------------------------------


def test_every_advertised_tool_has_a_handler():
    advertised = {t["name"] for t in TOOLS}
    assert advertised == set(TOOL_HANDLERS), (
        "tools/list and TOOL_HANDLERS disagree; a host would see a tool that "
        "cannot be called, or a callable tool it can never discover"
    )


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t["name"])
def test_advertised_tool_schema_is_well_formed(tool):
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert tool["description"].strip(), f"{tool['name']} has no description for the model to read"
    for required in schema.get("required", []):
        assert required in schema["properties"], (
            f"{tool['name']} requires '{required}' but never declares it"
        )


def test_search_tool_calls_the_store_with_parameters_the_store_accepts():
    """Guards the class of bug autospec exists to catch.

    ``search_archive`` takes a precomputed ``query_vec``; it has no
    ``lexical_only``. Passing one raised only at runtime, in a real host.
    """
    captured: dict = {}

    def fake_search(query, **kwargs):
        captured.update(kwargs)
        captured["_query"] = query
        return []

    with patch(
        "agentloom_runtime.session.store.search_archive", autospec=True, side_effect=fake_search
    ), patch(
        "agentloom_runtime.memory.embedding_provider.embed_query",
        autospec=True,
        return_value=[0.5],
    ):
        mcp_mod.tool_session_search({"query": "anything", "workspace_key": "github.com/a/b"})

    accepted = set(inspect.signature(store_mod.search_archive).parameters)
    assert set(captured) - {"_query"} <= accepted
    assert captured["query_vec"] == [0.5], "vector search must be attempted by default"


def test_search_tool_honours_lexical_only():
    """`lexical_only` must skip embedding entirely, not just ignore the result."""
    captured: dict = {}

    def fake_search(query, **kwargs):
        captured.update(kwargs)
        return []

    with patch(
        "agentloom_runtime.session.store.search_archive", autospec=True, side_effect=fake_search
    ), patch(
        "agentloom_runtime.memory.embedding_provider.embed_query", autospec=True
    ) as embed:
        mcp_mod.tool_session_search({"query": "anything", "lexical_only": True})

    embed.assert_not_called()
    assert captured["query_vec"] is None
