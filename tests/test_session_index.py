"""Archive index: prose-only chunking, hybrid ranking, pointer retrieval."""

from __future__ import annotations

from agentloom_runtime.session.index import (
    chunk_document,
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
