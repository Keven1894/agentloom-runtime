"""Transcript capture: redaction, normalization, rendering, and reader contract.

No database required. The redaction tests matter most — an agent transcript is
exactly where a credential that was echoed once would live forever.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloom_runtime.session.readers import READERS, discover_transcripts, get_reader
from agentloom_runtime.session.readers.base import (
    MAX_TOOL_VALUE_CHARS,
    TranscriptReader,
    summarize_tool_input,
)
from agentloom_runtime.session.readers.cursor import CursorTranscriptReader, workspace_slug
from agentloom_runtime.session.transcript import (
    TranscriptDocument,
    redact,
    render_markdown,
    render_text,
)


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret, text",
    [
        ("sk-abcdefghijklmnopqrstuvwxyz123456", "the key is sk-abcdefghijklmnopqrstuvwxyz123456"),
        ("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345", "token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("AKIAIOSFODNN7EXAMPLE", "aws AKIAIOSFODNN7EXAMPLE here"),
        ("xoxb-1234567890-abcdefghij", "slack xoxb-1234567890-abcdefghij"),
    ],
)
def test_provider_tokens_are_removed(secret, text):
    cleaned, hits = redact(text)
    assert secret not in cleaned
    assert hits == 1
    assert "[redacted:" in cleaned


def test_secret_assignments_are_removed_but_key_names_survive():
    cleaned, hits = redact('AGENTLOOM_DB_PASSWORD="dP7xQ!aZ22mW-K"')
    assert "dP7xQ!aZ22mW-K" not in cleaned
    assert "AGENTLOOM_DB_PASSWORD" in cleaned
    assert hits == 1


def test_connection_string_password_is_removed_but_host_survives():
    cleaned, _ = redact("mysql://admin:sup3rSecret@db.example.org:3306/prod")
    assert "sup3rSecret" not in cleaned
    assert "db.example.org:3306/prod" in cleaned
    assert "admin" in cleaned


def test_bearer_token_is_removed():
    cleaned, _ = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in cleaned


def test_private_key_block_is_removed_whole():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\nabc\n-----END RSA PRIVATE KEY-----"
    cleaned, hits = redact(text)
    assert "MIIEow" not in cleaned
    assert hits == 1


def test_ordinary_prose_is_left_alone():
    text = "We decided to keep the password policy at MEDIUM and move on."
    cleaned, hits = redact(text)
    assert cleaned == text
    assert hits == 0


def test_empty_text_is_safe():
    assert redact("") == ("", 0)


def test_redaction_is_idempotent():
    """Re-redacting must be a no-op, so "redact again, expect zero" is a valid audit.

    Without this, a placeholder like ``[redacted:secret-assignment]`` re-matches
    the very pattern that produced it and every audit reports false positives.
    """
    once, first = redact('DB_PASSWORD="sup3rSecretValue"')
    twice, second = redact(once)
    assert first == 1
    assert second == 0
    assert twice == once


def test_json_escaped_newline_is_not_mistaken_for_a_secret():
    """Serialized transcripts contain literal ``\\n``; that is whitespace, not a value."""
    cleaned, hits = redact(r'{"text":"DB_PASSWORD=\nexport NEXT=1"}')
    assert hits == 0
    assert cleaned == r'{"text":"DB_PASSWORD=\nexport NEXT=1"}'


def test_documentation_ellipsis_is_not_a_secret():
    cleaned, hits = redact("AWS_ACCESS_KEY_ID=...")
    assert hits == 0
    assert cleaned == "AWS_ACCESS_KEY_ID=..."


# --------------------------------------------------------------------------
# tool input summarization
# --------------------------------------------------------------------------


def test_short_fields_survive_and_bulky_ones_are_capped():
    summary, _ = summarize_tool_input({"path": "docs/plan/x.md", "contents": "y" * 5000})
    assert "path=docs/plan/x.md" in summary
    assert "y" * (MAX_TOOL_VALUE_CHARS + 1) not in summary
    assert "chars)" in summary


def test_tool_input_is_redacted_and_the_hit_is_reported():
    summary, hits = summarize_tool_input(
        {"command": "export TOKEN=sk-abcdefghijklmnopqrstuvwxyz01"}
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz01" not in summary
    assert hits == 1


def test_missing_tool_input_is_none():
    assert summarize_tool_input(None) == (None, 0)


def test_secrets_in_tool_arguments_count_toward_the_transcript_total(tmp_path):
    """A redaction total that ignores tool arguments would be misleading."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    root = tmp_path / "projects"
    _write_transcript(
        root,
        workspace_slug(workspace),
        "sec-1",
        [
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "running it"},
                        {
                            "type": "tool_use",
                            "name": "Shell",
                            "input": {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop'"},
                        },
                    ]
                },
            }
        ],
    )
    reader = CursorTranscriptReader(projects_root=root)
    doc = reader.read(reader.discover(workspace)[0])
    assert doc.redaction_count == 1
    assert "abcdefghijklmnop" not in json.dumps(doc.to_dict())


# --------------------------------------------------------------------------
# Cursor reader
# --------------------------------------------------------------------------


def test_workspace_slug_matches_the_observed_layout():
    assert workspace_slug(Path("C:/projects/widget")) == "c-projects-widget"
    assert workspace_slug(Path("/home/dev/my_repo")) == "home-dev-my-repo"


def _write_transcript(root: Path, slug: str, ref: str, lines: list[dict]) -> Path:
    folder = root / slug / "agent-transcripts" / ref
    folder.mkdir(parents=True)
    path = folder / f"{ref}.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8"
    )
    return path


def _sample_lines() -> list[dict]:
    return [
        {
            "role": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "<timestamp>x</timestamp>\n<user_query>\nWhy?\n</user_query>"}
                ]
            },
        },
        {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Because of the remote."},
                    {"type": "tool_use", "name": "Read", "input": {"path": "a.py"}},
                ]
            },
        },
        {"type": "turn_ended", "status": "ok", "error": None},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
    ]


def test_reader_normalizes_roles_and_unwraps_the_user_query(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    root = tmp_path / "projects"
    _write_transcript(root, workspace_slug(workspace), "abc-123", _sample_lines())

    reader = CursorTranscriptReader(projects_root=root)
    sources = reader.discover(workspace)
    assert [s.ref for s in sources] == ["abc-123"]

    doc = reader.read(sources[0])
    assert [t.role for t in doc.turns] == ["human", "agent", "agent"]
    assert doc.turns[0].text == "Why?"  # harness scaffolding stripped
    assert doc.turns[1].blocks[1].type == "tool_use"
    assert doc.turns[1].blocks[1].tool_name == "Read"


def test_bookkeeping_lines_do_not_become_turns(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    root = tmp_path / "projects"
    _write_transcript(root, workspace_slug(workspace), "abc-123", _sample_lines())
    doc = CursorTranscriptReader(projects_root=root).read(
        CursorTranscriptReader(projects_root=root).discover(workspace)[0]
    )
    assert doc.turn_count == 3


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    root = tmp_path / "projects"
    slug = workspace_slug(workspace)
    path = _write_transcript(root, slug, "abc-123", _sample_lines())
    path.write_text(
        path.read_text(encoding="utf-8") + "\nnot json at all\n{\"role\": 42}\n",
        encoding="utf-8",
    )
    reader = CursorTranscriptReader(projects_root=root)
    assert reader.read(reader.discover(workspace)[0]).turn_count == 3


def test_absent_host_yields_no_sources_rather_than_an_error(tmp_path):
    reader = CursorTranscriptReader(projects_root=tmp_path / "does-not-exist")
    assert reader.discover(tmp_path) == []
    assert discover_transcripts(tmp_path, host="nonexistent-host") == []


def test_registered_readers_satisfy_the_protocol():
    for reader in READERS:
        assert isinstance(reader, TranscriptReader)
        assert reader.host
        assert get_reader(reader.host) is reader


def test_readers_never_open_host_storage_for_writing():
    """A reader that writes can corrupt someone's editor. None may."""
    package = Path(__file__).resolve().parents[1] / "src" / "agentloom_runtime" / "session" / "readers"
    for path in package.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("write_text(", "open(\"w", "'w'", "unlink(", "rmtree("):
            assert forbidden not in source, f"{path.name} may modify host storage: {forbidden}"


# --------------------------------------------------------------------------
# document + rendering
# --------------------------------------------------------------------------


def _doc(tmp_path) -> TranscriptDocument:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    root = tmp_path / "projects"
    _write_transcript(root, workspace_slug(workspace), "abc-123", _sample_lines())
    reader = CursorTranscriptReader(projects_root=root)
    return reader.read(reader.discover(workspace)[0])


def test_document_survives_a_json_round_trip(tmp_path):
    doc = _doc(tmp_path)
    restored = TranscriptDocument.from_dict(json.loads(json.dumps(doc.to_dict())))
    assert restored.to_dict() == doc.to_dict()


def test_tail_keeps_the_most_recent_turns(tmp_path):
    doc = _doc(tmp_path)
    assert [t.seq for t in doc.tail(2).turns] == [2, 3]
    assert doc.tail(0).turn_count == 3
    assert doc.tail(99).turn_count == 3


def test_renderers_show_both_prose_and_tool_calls(tmp_path):
    doc = _doc(tmp_path)
    for rendered in (render_text(doc), render_markdown(doc)):
        assert "Why?" in rendered
        assert "Because of the remote." in rendered
        assert "Read" in rendered
