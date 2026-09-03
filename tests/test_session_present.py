"""Presentation overlay helpers: batching and JSON extraction, no network."""

from __future__ import annotations

import io
import urllib.error

from agentloom_runtime.session.present import (
    batched,
    parse_json_object,
    prose_items,
    retry_delay_seconds,
    split_pieces,
)
from agentloom_runtime.session.transcript import TranscriptBlock, TranscriptDocument, TranscriptTurn


def _doc(*turns: TranscriptTurn) -> TranscriptDocument:
    return TranscriptDocument(source_host="test", source_ref="t", turns=list(turns))


def _turn(seq: int, text: str) -> TranscriptTurn:
    return TranscriptTurn(
        seq=seq,
        role="human",
        blocks=[TranscriptBlock(type="text", text=text)],
    )


def test_parse_json_object_strips_fences_and_think():
    raw = "<think>nope</think>\n```json\n{\"7\": \"Hello\"}\n```\n"
    assert parse_json_object(raw) == {"7": "Hello"}


def test_parse_json_object_allows_raw_newlines_in_strings():
    raw = '{"7": "hello\nworld"}'
    assert parse_json_object(raw) == {"7": "hello\nworld"}


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    err = urllib.error.HTTPError("http://x/v1/chat/completions", code, "Not Found", hdrs=None, fp=io.BytesIO(body))
    err.body_text = err.read().decode()
    return err


def test_retry_delay_for_circuit_breaker_model_not_found():
    err = _http_error(
        404,
        b'{"detail":{"error":{"message":"The model \'qwen3:32b\' does not exist","code":"model_not_found"}}}',
    )
    assert retry_delay_seconds(err) == 30.0


def test_retry_delay_skips_plain_route_404():
    err = _http_error(404, b'{"detail":"Not Found"}')
    assert retry_delay_seconds(err) is None


def test_retry_delay_for_gateway_timeout_status():
    err = _http_error(504, b'{"detail":"gateway timeout"}')
    assert retry_delay_seconds(err) == 15.0


def test_batched_splits_on_char_budget():
    items = [{"seq": 1, "text": "aaaa"}, {"seq": 2, "text": "bbbb"}, {"seq": 3, "text": "c"}]
    groups = batched(items, max_chars=6)
    assert [[i["seq"] for i in g] for g in groups] == [[1], [2, 3]]


def test_split_pieces_leaves_short_turns_whole():
    pieces = split_pieces([{"seq": 4, "text": "短"}], max_chars=10)
    assert pieces == [{"seq": "4", "key": "4", "text": "短", "part": 0, "parts": 1}]


def test_split_pieces_cuts_a_long_turn_into_rejoinable_parts():
    text = "\n".join(f"line {i}" for i in range(20))
    pieces = split_pieces([{"seq": 7, "text": text}], max_chars=30)
    assert len(pieces) > 1
    assert [p["key"] for p in pieces] == [f"7#{i}" for i in range(len(pieces))]
    assert all(p["parts"] == len(pieces) for p in pieces)
    assert "".join(p["text"] for p in pieces) == text
    assert max(len(p["text"]) for p in pieces) <= 30


def test_split_pieces_hard_cuts_a_single_long_line():
    pieces = split_pieces([{"seq": 1, "text": "x" * 250}], max_chars=100)
    assert [len(p["text"]) for p in pieces] == [100, 100, 50]
    assert "".join(p["text"] for p in pieces) == "x" * 250


def test_batched_never_exceeds_budget_once_pieces_are_split():
    long_turn = {"seq": 1, "text": "y" * 5000}
    batches = batched(split_pieces([long_turn], max_chars=1000), max_chars=1000)
    assert max(sum(len(i["text"]) for i in b) for b in batches) <= 1000


def test_prose_items_can_keep_only_cjk():
    doc = _doc(_turn(1, "帮我查一下"), _turn(2, "just english"))
    assert [i["seq"] for i in prose_items(doc, cjk_only=True)] == [1]


def test_build_presentation_uses_chat_fn_without_a_network():
    from agentloom_runtime.session.present import build_presentation

    def fake_chat(messages):
        user = messages[0]["content"]
        if "Translate title" in user:
            return '{"title": {"en": "Check logging", "es": "Revisar el registro"}, "description": {"en": "Ask about logs", "es": "Preguntar por los logs"}}'
        if "English" in user:
            return '{"1": "Please look this up"}'
        return '{"1": "Por favor busca esto"}'

    pack = build_presentation(_doc(_turn(1, "帮我查一下这个")), chat_fn=fake_chat)
    assert pack["title"]["en"] == "Check logging"
    assert pack["turns"]["en"]["1"] == ["Please look this up"]
    assert pack["turns"]["es"]["1"] == ["Por favor busca esto"]


def test_parse_json_object_recovers_dropped_closing_quote():
    """Qwen ends a backtick-quoted value and jumps to `}` without closing the string."""
    raw = '{\n  "7": "see `docs/x.md`\n}'

    assert parse_json_object(raw) == {"7": "see `docs/x.md`"}


def test_parse_json_object_repairs_invalid_backslash_escapes():
    raw = r'{"1": "path C:\projects\envistor-data and \env file"}'

    assert parse_json_object(raw) == {
        "1": r"path C:\projects\envistor-data and \env file"
    }


def test_parse_json_object_still_rejects_real_garbage():
    import pytest

    with pytest.raises(ValueError):
        parse_json_object("not json at all")
