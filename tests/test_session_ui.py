"""The Layer 0 session web viewer.

Patches bind ``autospec=True`` so a handler calling the store with an argument
the store does not accept fails here rather than in a browser.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from agentloom_runtime.session.index import ArchiveHit
from agentloom_runtime.session.store import SessionRecord
from agentloom_runtime.session.transcript import TranscriptBlock, TranscriptDocument, TranscriptTurn
from agentloom_runtime.session.ui.server import HTML_TEMPLATE, SessionApiHandler, _int_param


class DummyRequest:
    def __init__(self, path: str):
        self.path = path

    def makefile(self, *args, **kwargs):
        if "w" in args[0] or "wb" in args[0]:
            return io.BytesIO()
        return io.BytesIO(f"GET {self.path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("utf-8"))


def _call_handler(path: str) -> tuple[int, dict, bytes]:
    """Helper to simulate an HTTP GET request to SessionApiHandler."""
    req = DummyRequest(path)
    handler = SessionApiHandler.__new__(SessionApiHandler)
    handler.rfile = req.makefile("rb")
    handler.wfile = io.BytesIO()
    handler.raw_requestline = handler.rfile.readline()
    handler.parse_request()
    handler.path = path
    handler.do_GET()
    response_bytes = handler.wfile.getvalue()
    return response_bytes


def test_ui_index_html_renders():
    assert "AgentLoom" in HTML_TEMPLATE
    assert "Session DAG" in HTML_TEMPLATE
    assert "Transcripts" in HTML_TEMPLATE


def test_ui_api_workspaces():
    mock_sessions = [
        SessionRecord(
            session_id="s-1",
            agent_id="a-1",
            operator_id="o-1",
            workspace_key="github.com/acme/repo1",
            status="open",
        ),
        SessionRecord(
            session_id="s-2",
            agent_id="a-1",
            operator_id="o-1",
            workspace_key="github.com/acme/repo2",
            status="parked",
        ),
    ]
    with patch(
        "agentloom_runtime.session.store.search_sessions",
        autospec=True,
        return_value=mock_sessions,
    ):
        resp_bytes = _call_handler("/api/workspaces")
        assert b"200 OK" in resp_bytes
        body = resp_bytes.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        assert "github.com/acme/repo1" in data
        assert "github.com/acme/repo2" in data


def test_ui_api_sessions():
    tree = [{"session_id": "s-1", "children": []}]
    with patch(
        "agentloom_runtime.session.store.get_workspace_session_tree",
        autospec=True,
        return_value=tree,
    ):
        resp_bytes = _call_handler("/api/sessions?workspace=github.com/acme/repo1")
        assert b"200 OK" in resp_bytes
        body = resp_bytes.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        assert len(data) == 1
        assert data[0]["session_id"] == "s-1"


def test_ui_api_transcript_detail():
    doc = TranscriptDocument(
        source_host="cursor",
        source_ref="ref-123",
        turns=[TranscriptTurn(seq=1, role="human", blocks=[TranscriptBlock(type="text", text="hi")])],
    )
    with patch(
        "agentloom_runtime.session.store.load_transcript", autospec=True, return_value=doc
    ):
        resp_bytes = _call_handler("/api/transcripts/t-1")
        assert b"200 OK" in resp_bytes
        body = resp_bytes.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        assert data["source_ref"] == "ref-123"
        assert len(data["turns"]) == 1


def _hit() -> ArchiveHit:
    return ArchiveHit(
        chunk_id="c-1",
        transcript_id="t-1",
        source_host="cursor",
        source_ref="ref-123",
        workspace_key="github.com/acme/repo1",
        granularity="window",
        seq_start=1,
        seq_end=5,
        score=0.9,
        snippet="decided on dag structure",
        search_mode="hybrid",
        captured_at=None,
    )


def test_ui_api_search():
    with patch(
        "agentloom_runtime.session.store.search_archive", autospec=True, return_value=[_hit()]
    ), patch(
        "agentloom_runtime.memory.embedding_provider.embed_query",
        autospec=True,
        return_value=[0.1],
    ):
        resp_bytes = _call_handler("/api/search?q=dag&workspace=github.com/acme/repo1")
        assert b"200 OK" in resp_bytes
        body = resp_bytes.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        assert len(data) == 1
        assert data[0]["snippet"] == "decided on dag structure"


# --------------------------------------------------------------------------
# the viewer must not quietly ignore what the URL asked for
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 12),  # absent -> default
        ("3", 3),
        ("0", 1),  # clamped up
        ("-5", 1),
        ("9999", 100),  # clamped down
        ("abc", 12),  # unparseable -> default
    ],
)
def test_int_param_clamps_instead_of_failing(raw, expected):
    query = {} if raw is None else {"limit": [raw]}
    assert _int_param(query, "limit", 12, maximum=100) == expected


def test_search_honours_the_requested_limit():
    """The limit was hardcoded, so a request for 2 results returned 12."""
    captured: dict = {}

    def fake_search(query, **kwargs):
        captured.update(kwargs)
        return [_hit()]

    with patch(
        "agentloom_runtime.session.store.search_archive", autospec=True, side_effect=fake_search
    ), patch(
        "agentloom_runtime.memory.embedding_provider.embed_query",
        autospec=True,
        return_value=[0.1],
    ):
        _call_handler("/api/search?q=dag&limit=2")

    assert captured["limit"] == 2


def test_transcript_listing_honours_the_requested_limit():
    captured: dict = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return []

    with patch(
        "agentloom_runtime.session.store.list_transcripts", autospec=True, side_effect=fake_list
    ):
        _call_handler("/api/transcripts?limit=7")

    assert captured["limit"] == 7


def test_viewer_search_uses_the_archives_embeddings():
    """The viewer is the human-facing surface.

    Omitting the query vector made it silently lexical-only against an archive
    that is fully embedded — the worst kind of failure, since results still
    look plausible.
    """
    captured: dict = {}

    def fake_search(query, **kwargs):
        captured.update(kwargs)
        return []

    with patch(
        "agentloom_runtime.session.store.search_archive", autospec=True, side_effect=fake_search
    ), patch(
        "agentloom_runtime.memory.embedding_provider.embed_query",
        autospec=True,
        return_value=[0.25],
    ):
        _call_handler("/api/search?q=dag")

    assert captured["query_vec"] == [0.25]


def test_viewer_search_can_be_forced_lexical():
    captured: dict = {}

    def fake_search(query, **kwargs):
        captured.update(kwargs)
        return []

    with patch(
        "agentloom_runtime.session.store.search_archive", autospec=True, side_effect=fake_search
    ), patch("agentloom_runtime.memory.embedding_provider.embed_query", autospec=True) as embed:
        _call_handler("/api/search?q=dag&lexical=1")

    embed.assert_not_called()
    assert captured["query_vec"] is None
