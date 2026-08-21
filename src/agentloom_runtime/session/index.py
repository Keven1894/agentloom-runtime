"""Archive index: locate a conversation without loading it.

Checkpoints answer "what was I doing". The transcript archive answers "what
exactly was said". This module is the locator between them: it turns a natural
query into ``(transcript_id, seq)`` pointers, which ``replay --around`` then
pages.

Design constraints from the memory-architecture literature (and from our own
measured transcripts):

- Index **human and agent prose only**. Tool-call arguments dominate the bytes
  and flatten into near-duplicate chunks.
- Two granularities: one session-level summary node, plus overlapping turn
  windows. Coarse first, then fine — the property that makes hierarchical
  memory win the benchmarks.
- Hybrid lexical + vector, fused with RRF. Identifiers and error strings are
  exact-match objects; prose is semantic. Time is a filter column, not a hope
  that cosine encodes chronology.
- Return pointers, not generated summaries. The archive stays the source of
  truth.

Chunking and ranking here have no database dependency, so they can be tested
with fake vectors. Persistence lives in :mod:`agentloom_runtime.session.store`.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from agentloom_runtime.memory.joint_retrieval import DEFAULT_RRF_K, reciprocal_rank_fusion
from agentloom_runtime.session.transcript import TranscriptDocument, TranscriptTurn

__all__ = [
    "MAX_CONTENT_CHARS",
    "MIN_PROSE_CHARS",
    "VECTOR_DTYPE",
    "WINDOW_SIZE",
    "WINDOW_STRIDE",
    "ArchiveHit",
    "TranscriptChunk",
    "chunk_document",
    "decode_vector",
    "encode_vector",
    "hybrid_rank",
    "lexical_rank",
    "prose_turns",
    "tokenize_query",
    "vector_rank",
]

WINDOW_SIZE = 5
WINDOW_STRIDE = 3
MAX_CONTENT_CHARS = 6000
MIN_PROSE_CHARS = 40

# Little-endian float32. Pinned rather than native because these vectors are
# written and read across architectures — the same archive is used from x86-64
# and aarch64 — and a native-order round trip would corrupt silently.
VECTOR_DTYPE = "<f4"

_TOKEN = re.compile(r"[A-Za-z0-9_./:@-]+|[\u4e00-\u9fff]+")
_ROLE_LABEL = {"human": "Human", "agent": "Agent", "system": "System"}


@dataclass
class TranscriptChunk:
    """One indexable slice of a conversation, already redacted (the archive is)."""

    granularity: str  # "session" | "window"
    seq_start: int
    seq_end: int
    content: str
    content_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.content_sha256:
            self.content_sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArchiveHit:
    chunk_id: str
    transcript_id: str
    source_host: str
    source_ref: str
    workspace_key: str
    granularity: str
    seq_start: int
    seq_end: int
    captured_at: Optional[str]
    score: float
    snippet: str
    search_mode: str
    content: str = field(repr=False, default="")

    @property
    def seq(self) -> int:
        """Centre of the hit, for ``replay --around``."""
        return (self.seq_start + self.seq_end) // 2

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["seq"] = self.seq
        payload.pop("content", None)
        return payload


def prose_turns(doc: TranscriptDocument) -> list[TranscriptTurn]:
    """Turns that have readable text. Tool-only turns are dropped."""
    return [t for t in doc.turns if (t.text or "").strip()]


def _format_turn(turn: TranscriptTurn) -> str:
    label = _ROLE_LABEL.get(turn.role, turn.role)
    return f"[{turn.seq}] {label}: {turn.text.strip()}"


def _clip(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def chunk_document(
    doc: TranscriptDocument,
    *,
    window_size: int = WINDOW_SIZE,
    stride: int = WINDOW_STRIDE,
) -> list[TranscriptChunk]:
    """Build the session node plus overlapping prose windows.

    Empty conversations yield no chunks. A conversation with fewer prose turns
    than the window size still gets a session node, and one window covering
    whatever prose exists.
    """
    prose = prose_turns(doc)
    if not prose:
        return []

    chunks: list[TranscriptChunk] = []
    session_text = _clip("\n\n".join(_format_turn(t) for t in prose))
    if len(session_text) >= MIN_PROSE_CHARS:
        chunks.append(
            TranscriptChunk(
                granularity="session",
                seq_start=prose[0].seq,
                seq_end=prose[-1].seq,
                content=session_text,
            )
        )

    size = min(window_size, len(prose))
    starts = list(range(0, max(len(prose) - size, 0) + 1, stride))
    if not starts:
        starts = [0]
    # Always include a window that covers the tail.
    last_start = len(prose) - size
    if last_start > 0 and last_start not in starts:
        starts.append(last_start)

    seen: set[tuple[int, int]] = set()
    for start in starts:
        window = prose[start : start + size]
        seq_start, seq_end = window[0].seq, window[-1].seq
        if (seq_start, seq_end) in seen:
            continue
        seen.add((seq_start, seq_end))
        text = _clip("\n\n".join(_format_turn(t) for t in window))
        if len(text) < MIN_PROSE_CHARS:
            continue
        chunks.append(
            TranscriptChunk(
                granularity="window",
                seq_start=seq_start,
                seq_end=seq_end,
                content=text,
            )
        )
    return chunks


def tokenize_query(query: str) -> list[str]:
    """ASCII identifiers plus CJK runs. CJK runs also emit character bigrams."""
    tokens: list[str] = []
    for match in _TOKEN.finditer(query or ""):
        raw = match.group(0)
        if not raw:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            if len(raw) >= 1:
                tokens.append(raw)
            tokens.extend(raw[i : i + 2] for i in range(len(raw) - 1))
        elif len(raw) >= 2:
            tokens.append(raw.lower())
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def lexical_rank(query: str, items: list[tuple[str, str]]) -> list[tuple[str, float]]:
    """Substring coverage. Paths, error strings, and CJK phrases all hit this."""
    tokens = tokenize_query(query)
    if not tokens:
        return []
    ranked: list[tuple[str, float]] = []
    for key, content in items:
        haystack = content.lower()
        hits = sum(1 for token in tokens if token.lower() in haystack)
        if hits:
            ranked.append((key, hits / len(tokens)))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def encode_vector(vec: Optional[list[float]]) -> Optional[bytes]:
    """Pack an embedding for storage. ``None`` and empty stay ``None``."""
    if not vec:
        return None
    return _np().asarray(vec, dtype=VECTOR_DTYPE).tobytes()


def decode_vector(blob: Optional[bytes], dim: Optional[int] = None) -> Optional[list[float]]:
    """Unpack a stored embedding.

    A truncated or wrong-width buffer returns ``None`` rather than a plausible
    vector of the wrong length: a silently mis-decoded embedding would rank
    results confidently and wrongly, which is harder to notice than a miss.
    """
    if not blob:
        return None
    if len(blob) % 4:
        return None
    values = _np().frombuffer(blob, dtype=VECTOR_DTYPE)
    if dim is not None and len(values) != dim:
        return None
    return values.tolist()


def _np():
    import numpy

    return numpy


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def vector_rank(
    query_vec: list[float],
    items: list[tuple[str, list[float]]],
) -> list[tuple[str, float]]:
    """Cosine similarity of every candidate against the query.

    Scored as one matrix operation. Vectors of an unexpected width are dropped
    rather than reshaped — a mismatch means a different embedding model, and
    comparing across models produces numbers that look like similarities.
    """
    usable = [(key, vec) for key, vec in items if vec]
    if not usable or not query_vec:
        return []

    numpy = _np()
    width = len(query_vec)
    usable = [(key, vec) for key, vec in usable if len(vec) == width]
    if not usable:
        return []

    matrix = numpy.asarray([vec for _, vec in usable], dtype=VECTOR_DTYPE)
    query = numpy.asarray(query_vec, dtype=VECTOR_DTYPE)

    norms = numpy.linalg.norm(matrix, axis=1)
    query_norm = numpy.linalg.norm(query)
    if query_norm == 0:
        return []
    # Zero-norm rows would divide by zero; they score 0 by definition.
    safe = numpy.where(norms == 0, 1.0, norms)
    scores = (matrix @ query) / (safe * query_norm)
    scores = numpy.where(norms == 0, 0.0, scores)

    ranked = list(zip((key for key, _ in usable), (float(s) for s in scores)))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked


def hybrid_rank(
    query: str,
    items: list[dict[str, Any]],
    *,
    query_vec: Optional[list[float]] = None,
    limit: int = 8,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[dict[str, Any]]:
    """Fuse lexical and (optional) vector rankings. Each item needs ``id`` and ``content``.

    When a session-level and a window-level chunk hit the same transcript, the
    window is kept (it is the more precise pointer) and inherits the session
    node's RRF mass so the conversation is not listed twice.
    """
    lexical_items = [(item["id"], item.get("content") or "") for item in items]
    lex = lexical_rank(query, lexical_items)

    lists: list[list[tuple[str, dict[str, Any]]]] = [
        [(key, {"score": score, "mode": "lexical"}) for key, score in lex]
    ]
    if query_vec:
        vector_items = [
            (item["id"], item["embedding"])
            for item in items
            if item.get("embedding")
        ]
        vec = vector_rank(query_vec, vector_items)
        lists.append([(key, {"score": score, "mode": "vector"}) for key, score in vec])

    fused = reciprocal_rank_fusion(lists, rrf_k=rrf_k)
    by_id = {item["id"]: item for item in items}

    # Collapse session+window for the same transcript: keep the best window,
    # or the session node if no window ranked. One pointer per conversation.
    best_for_transcript: dict[str, tuple[str, float, dict[str, Any]]] = {}
    for key, score, payload in fused:
        item = by_id.get(key)
        if item is None:
            continue
        tid = item.get("transcript_id") or key
        current = best_for_transcript.get(tid)
        if current is None:
            best_for_transcript[tid] = (key, score, payload)
            continue
        _, cur_score, _ = current
        cur_item = by_id[current[0]]
        prefer_window = (
            item.get("granularity") == "window"
            and cur_item.get("granularity") != "window"
        )
        prefer_higher = (
            item.get("granularity") == cur_item.get("granularity") and score > cur_score
        )
        if prefer_window or prefer_higher:
            best_for_transcript[tid] = (key, max(score, cur_score), payload)

    merged = list(best_for_transcript.values())
    merged.sort(key=lambda row: row[1], reverse=True)

    results: list[dict[str, Any]] = []
    for key, score, payload in merged[:limit]:
        item = dict(by_id[key])
        modes = {payload.get("mode", "hybrid")}
        item["score"] = score
        item["search_mode"] = "hybrid" if query_vec else "lexical"
        item["_modes"] = modes
        results.append(item)
    return results


def snippet(content: str, query: str, width: int = 180) -> str:
    """A short excerpt around the first query token that hits."""
    text = " ".join((content or "").split())
    if not text:
        return ""
    tokens = tokenize_query(query)
    lower = text.lower()
    pos = 0
    for token in tokens:
        found = lower.find(token.lower())
        if found >= 0:
            pos = found
            break
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    excerpt = text[start:end]
    if start:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt = excerpt + "…"
    return excerpt
