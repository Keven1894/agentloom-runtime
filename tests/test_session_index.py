"""Archive index: prose-only chunking, hybrid ranking, pointer retrieval."""

from __future__ import annotations

import struct

import pytest

from agentloom_runtime.session.index import (
    apply_turn_overlay,
    chunk_document,
    decode_vector,
    encode_vector,
    hybrid_rank,
    lexical_rank,
    prose_turns,
    tokenize_query,
    vector_rank,
)
from agentloom_runtime.session.transcript import (
    TranscriptBlock,
    TranscriptDocument,
    TranscriptTurn,
)


def _turn(seq: int, role: str, text: str | None = None, tool: str | None = None) -> TranscriptTurn:
    blocks = []
    if text:
        blocks.append(TranscriptBlock(type="text", text=text))
    if tool:
        blocks.append(TranscriptBlock(type="tool_use", tool_name=tool, tool_input="path=x.py"))
    return TranscriptTurn(seq=seq, role=role, blocks=blocks)


def _doc(*turns: TranscriptTurn) -> TranscriptDocument:
    return TranscriptDocument(source_host="cursor", source_ref="abc", turns=list(turns))


def test_apply_turn_overlay_replaces_text_and_keeps_tools():
    doc = _doc(
        _turn(1, "human", "来"),
        _turn(2, "agent", "好", tool="Read"),
    )
    out = apply_turn_overlay(doc, {"1": ["Come"], "2": ["OK"]})
    assert out.turns[0].text == "Come"
    assert out.turns[1].text == "OK"
    assert out.turns[1].blocks[1].tool_name == "Read"


def test_session_chunk_heading_is_searchable_listing_copy():
    chunks = chunk_document(
        _doc(_turn(1, "human", "hello there friends")),
        heading="Presupuesto de API de modelos frontier",
        locale="es",
    )
    session = next(c for c in chunks if c.granularity == "session")
    assert session.locale == "es"
    assert "Presupuesto de API" in session.content
    assert "hello there friends" in session.content


def test_prose_turns_drop_tool_only_noise():
    doc = _doc(
        _turn(1, "human", "Why the VCS remote?"),
        _turn(2, "agent", tool="Read"),
        _turn(3, "agent", "Because identity is the remote, not the path."),
        _turn(4, "agent", tool="StrReplace"),
    )
    assert [t.seq for t in prose_turns(doc)] == [1, 3]


def test_chunker_emits_session_and_windows_over_prose_only():
    turns = [_turn(i, "human" if i % 2 else "agent", f"decision {i} about indexing") for i in range(1, 12)]
    turns.insert(2, _turn(99, "agent", tool="Shell"))  # seq 99, tool-only, must not enter windows
    chunks = chunk_document(_doc(*turns), window_size=3, stride=3)
    granularities = {c.granularity for c in chunks}
    assert granularities == {"session", "window"}
    session = next(c for c in chunks if c.granularity == "session")
    assert "decision 1" in session.content
    assert "tool_use" not in session.content
    assert "Shell" not in session.content
    windows = [c for c in chunks if c.granularity == "window"]
    assert windows
    for window in windows:
        assert window.seq_end >= window.seq_start
        assert "Shell" not in window.content


def test_around_keeps_seq_neighborhood():
    doc = _doc(*[_turn(i, "human", f"t{i}") for i in range(1, 21)])
    sliced = doc.around(10, radius=2)
    assert [t.seq for t in sliced.turns] == [8, 9, 10, 11, 12]


def test_tokenize_keeps_cjk_and_identifiers():
    tokens = tokenize_query("密码策略 AGENTLOOM_DB_PASSWORD")
    assert "密码策略" in tokens or "密码" in tokens
    assert "agentloom_db_password" in tokens


def test_lexical_rank_prefers_the_matching_chunk():
    ranked = lexical_rank(
        "password policy MEDIUM",
        [
            ("a", "We decided the password policy stays at MEDIUM."),
            ("b", "Apply the migration to agentloom_dev."),
        ],
    )
    assert ranked[0][0] == "a"
    assert ranked[0][1] > 0


def test_hybrid_rank_spanish_query_prefers_spanish_overlay_window():
    """Cross-language retrieval is why overlays are indexed, not just displayed."""
    items = [
        {
            "id": "orig",
            "transcript_id": "t1",
            "granularity": "window",
            "locale": "original",
            "content": "FIU Libraries frontier model API budget of seven thousand six hundred dollars",
        },
        {
            "id": "es",
            "transcript_id": "t1",
            "granularity": "window",
            "locale": "es",
            "content": "Presupuesto de API de modelos frontier para las bibliotecas de FIU, siete mil seiscientos dolares",
        },
    ]
    ranked = hybrid_rank("presupuesto bibliotecas frontier", items, limit=8)
    assert ranked[0]["id"] == "es"
    assert ranked[0]["locale"] == "es"


def test_vector_rank_uses_cosine():
    ranked = vector_rank(
        [1.0, 0.0],
        [("close", [0.9, 0.1]), ("far", [0.0, 1.0])],
    )
    assert [k for k, _ in ranked] == ["close", "far"]


def test_hybrid_rank_returns_one_window_pointer_per_transcript():
    items = [
        {
            "id": "s1",
            "transcript_id": "t1",
            "granularity": "session",
            "content": "We decided the password policy stays MEDIUM and moved on.",
            "embedding": [1.0, 0.0],
            "seq_start": 1,
            "seq_end": 80,
        },
        {
            "id": "w1",
            "transcript_id": "t1",
            "granularity": "window",
            "content": "password policy stays MEDIUM",
            "embedding": [1.0, 0.0],
            "seq_start": 12,
            "seq_end": 16,
        },
        {
            "id": "w2",
            "transcript_id": "t2",
            "granularity": "window",
            "content": "unrelated migration notes",
            "embedding": [0.0, 1.0],
            "seq_start": 1,
            "seq_end": 5,
        },
    ]
    hits = hybrid_rank("password policy MEDIUM", items, query_vec=[1.0, 0.0], limit=8)
    t1 = [h for h in hits if h["transcript_id"] == "t1"]
    assert len(t1) == 1
    assert t1[0]["granularity"] == "window"
    assert t1[0]["seq_start"] == 12


# --------------------------------------------------------------------------
# vector storage — the archive is written and read across architectures
# --------------------------------------------------------------------------


def test_vector_survives_an_encode_decode_round_trip():
    vec = [0.5, -0.25, 0.125, 0.0]
    restored = decode_vector(encode_vector(vec), dim=len(vec))
    assert restored == pytest.approx(vec)


def test_encoding_is_little_endian_float32_regardless_of_host():
    """Byte order is pinned, not native.

    The same rows are written from x86-64 and read from aarch64. A native-order
    encoding would round-trip perfectly on one machine and return garbage
    similarities on the other.
    """
    blob = encode_vector([1.0])
    assert blob == struct.pack("<f", 1.0)
    assert len(encode_vector([0.0] * 1536)) == 1536 * 4


def test_empty_and_missing_vectors_decode_to_none():
    assert encode_vector(None) is None
    assert encode_vector([]) is None
    assert decode_vector(None) is None
    assert decode_vector(b"") is None


def test_a_corrupt_buffer_decodes_to_none_rather_than_a_plausible_vector():
    """A truncated buffer must not become a shorter vector.

    Silently returning the wrong width would rank confidently against the wrong
    dimensions instead of reporting that the row is unusable.
    """
    assert decode_vector(b"\x00\x00\x00") is None  # not a whole number of floats
    assert decode_vector(encode_vector([1.0, 2.0]), dim=1536) is None


def test_vector_rank_orders_by_cosine_similarity():
    query = [1.0, 0.0]
    ranked = vector_rank(query, [("opposite", [-1.0, 0.0]),
                                 ("same", [1.0, 0.0]),
                                 ("orthogonal", [0.0, 1.0])])
    assert [key for key, _ in ranked] == ["same", "orthogonal", "opposite"]
    assert ranked[0][1] == pytest.approx(1.0)
    assert ranked[1][1] == pytest.approx(0.0)


def test_vector_rank_ignores_vectors_of_a_different_width():
    """A width mismatch means a different embedding model.

    Comparing across models yields numbers that look like similarities, so the
    rows are dropped instead.
    """
    ranked = vector_rank([1.0, 0.0], [("ok", [1.0, 0.0]), ("other-model", [1.0, 0.0, 0.0])])
    assert [key for key, _ in ranked] == ["ok"]


def test_vector_rank_tolerates_a_zero_vector():
    ranked = dict(vector_rank([1.0, 0.0], [("zero", [0.0, 0.0]), ("same", [1.0, 0.0])]))
    assert ranked["zero"] == 0.0
    assert ranked["same"] == pytest.approx(1.0)
