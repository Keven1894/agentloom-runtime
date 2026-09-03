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
from agentloom_runtime.session.store import SessionRecord, TranscriptRecord
from agentloom_runtime.session.transcript import TranscriptBlock, TranscriptDocument, TranscriptTurn
from agentloom_runtime.session.ui.server import HTML_TEMPLATE, SessionApiHandler, _int_param


class DummyRequest:
    def __init__(self, path: str, method: str = "GET", body: bytes = b""):
        self.path = path
        self.method = method
        self.body = body

    def makefile(self, *args, **kwargs):
        if "w" in args[0] or "wb" in args[0]:
            return io.BytesIO()
        headers = f"{self.method} {self.path} HTTP/1.1\r\nHost: localhost\r\n"
        if self.body:
            headers += (
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(self.body)}\r\n"
            )
        return io.BytesIO(headers.encode("utf-8") + b"\r\n" + self.body)


def _call_handler(path: str, method: str = "GET", json_body: dict | None = None) -> bytes:
    """Simulate one request to SessionApiHandler."""
    body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
    req = DummyRequest(path, method=method, body=body)
    handler = SessionApiHandler.__new__(SessionApiHandler)
    handler.rfile = req.makefile("rb")
    handler.wfile = io.BytesIO()
    handler.raw_requestline = handler.rfile.readline()
    handler.parse_request()
    handler.path = path
    if method == "PATCH":
        handler.do_PATCH()
    else:
        handler.do_GET()
    return handler.wfile.getvalue()


def test_ui_index_html_renders():
    assert "AgentLoom" in HTML_TEMPLATE
    assert "Session DAG" in HTML_TEMPLATE
    assert "Transcripts" in HTML_TEMPLATE
    assert "saveTitle" in HTML_TEMPLATE
    assert "/title" in HTML_TEMPLATE
    assert "stripped at archive time" in HTML_TEMPLATE
    assert "You renamed this conversation" in HTML_TEMPLATE
    assert "showHoverTip" in HTML_TEMPLATE
    assert "Paused, not finished" in HTML_TEMPLATE
    assert "Not a filter on the Transcripts list" in HTML_TEMPLATE
    assert "statusTip" in HTML_TEMPLATE
    assert "unknown-host" in HTML_TEMPLATE
    assert "/api/identity" in HTML_TEMPLATE
    assert "open last on" in HTML_TEMPLATE
    assert "checkpointHostTip" in HTML_TEMPLATE
    assert "Append-only" in HTML_TEMPLATE
    assert "marked.min.js" in HTML_TEMPLATE
    assert "purify.min.js" in HTML_TEMPLATE
    assert "renderMarkdown" in HTML_TEMPLATE
    assert "setUiLocale" in HTML_TEMPLATE
    assert ">Original<" in HTML_TEMPLATE
    assert ">English<" in HTML_TEMPLATE
    assert ">Spanish<" in HTML_TEMPLATE
    assert 'v-html="renderMarkdown' in HTML_TEMPLATE


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


def test_ui_api_identity():
    from agentloom_runtime.session.identity import HostContext

    fake = HostContext(
        host_hint="GISBG",
        ide_hint="cursor",
        workspace_path_hint=r"C:\projects\envistor-data",
    )
    with patch(
        "agentloom_runtime.session.ui.server.detect_host_context",
        autospec=True,
        return_value=fake,
    ), patch(
        "agentloom_runtime.session.ui.server.detect_workspace_key",
        autospec=True,
        return_value="example.com/org/repo",
    ):
        resp_bytes = _call_handler("/api/identity")
        assert b"200 OK" in resp_bytes
        body = resp_bytes.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        assert data["host_hint"] == "GISBG"
        assert data["ide_hint"] == "cursor"
        assert data["workspace_key"] == "example.com/org/repo"


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
    ), patch(
        "agentloom_runtime.session.store.get_transcript_record",
        autospec=True,
        return_value=TranscriptRecord(
            transcript_id="t-1",
            session_id=None,
            source_host="cursor",
            source_ref="ref-123",
            workspace_key="ws",
            title="Renamed in the archive",
            title_source="user",
        ),
    ):
        resp_bytes = _call_handler("/api/transcripts/t-1")
        assert b"200 OK" in resp_bytes
        body = resp_bytes.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        assert data["source_ref"] == "ref-123"
        assert len(data["turns"]) == 1
        assert data["title"] == "Renamed in the archive"
        assert data["title_source"] == "user"
        assert data["presentation"] is None


def test_ui_api_transcript_detail_includes_presentation():
    doc = TranscriptDocument(
        source_host="cursor",
        source_ref="ref-123",
        turns=[TranscriptTurn(seq=1, role="human", blocks=[TranscriptBlock(type="text", text="hi")])],
    )
    pack = {
        "title": {"original": "预算", "en": "Budget", "es": "Presupuesto"},
        "description": {"en": "A short note."},
    }
    with patch(
        "agentloom_runtime.session.store.load_transcript", autospec=True, return_value=doc
    ), patch(
        "agentloom_runtime.session.store.get_transcript_record",
        autospec=True,
        return_value=TranscriptRecord(
            transcript_id="t-1",
            session_id=None,
            source_host="cursor",
            source_ref="ref-123",
            workspace_key="ws",
            title="预算",
            presentation=pack,
        ),
    ):
        resp_bytes = _call_handler("/api/transcripts/t-1")
        body = resp_bytes.split(b"\r\n\r\n", 1)[1]
        data = json.loads(body)
        assert data["presentation"]["title"]["en"] == "Budget"
        assert data["presentation"]["description"]["en"] == "A short note."


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
    ), patch(
        "agentloom_runtime.session.store.count_transcripts", autospec=True, return_value=42
    ):
        resp_bytes = _call_handler("/api/transcripts?limit=7&offset=14")

    assert captured["limit"] == 7
    assert captured["offset"] == 14
    body = resp_bytes.split(b"\r\n\r\n", 1)[1]
    data = json.loads(body)
    assert data["total"] == 42
    assert data["limit"] == 7
    assert data["offset"] == 14
    assert isinstance(data["items"], list)


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


def test_rename_transcript_uses_patch_and_does_not_touch_the_body():
    captured: dict = {}

    def fake_set(transcript_id, title):
        captured["transcript_id"] = transcript_id
        captured["title"] = title
        return TranscriptRecord(
            transcript_id=transcript_id,
            session_id=None,
            source_host="cursor",
            source_ref="ref-123",
            workspace_key="ws",
            title="Cross-machine session sync",
            title_source="user",
        )

    with patch(
        "agentloom_runtime.session.store.set_transcript_title",
        autospec=True,
        side_effect=fake_set,
    ):
        resp = _call_handler(
            "/api/transcripts/t-1/title",
            method="PATCH",
            json_body={"title": "Cross-machine session sync"},
        )

    assert b"200 OK" in resp
    assert captured == {"transcript_id": "t-1", "title": "Cross-machine session sync"}
    data = json.loads(resp.split(b"\r\n\r\n", 1)[1])
    assert data["title"] == "Cross-machine session sync"
    assert data["title_source"] == "user"


def test_rename_missing_transcript_is_404():
    with patch(
        "agentloom_runtime.session.store.set_transcript_title",
        autospec=True,
        side_effect=KeyError("t-missing"),
    ):
        resp = _call_handler(
            "/api/transcripts/t-missing/title",
            method="PATCH",
            json_body={"title": "nope"},
        )
    assert b"404" in resp.split(b"\r\n", 1)[0]

