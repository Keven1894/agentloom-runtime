"""Normalized conversation transcripts.

Layer 0 checkpoints answer "what was I doing" in a few hundred bytes that load
on every session start. Transcripts answer "what exactly was said, and why did
we decide that" in a few hundred kilobytes that are far too large to load every
time. They are complementary: the checkpoint is the index, the transcript is the
archive you page into on demand.

Every host records conversations in its own format, so capture is inherently
host-specific — see :mod:`agentloom_runtime.session.readers`. Everything from
the normalized document inward is host-neutral: one schema, one redaction pass,
one renderer.

Content blocks follow the shape most agent hosts already use — ``text`` and
``tool_use`` — so normalization is usually a re-labelling rather than a
translation. Tool *results* are deliberately not modelled: they are large,
they are the most likely place for a credential to appear, and they are rarely
what someone re-reads a conversation for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

__all__ = [
    "REDACTION_PATTERNS",
    "TranscriptBlock",
    "TranscriptDocument",
    "TranscriptTurn",
    "redact",
    "render_markdown",
    "render_text",
]

ROLES = {"human", "agent", "system"}

# Pattern-based redaction. Anything matching is replaced before the transcript
# is stored, because an agent transcript is exactly where a credential that was
# echoed once lives forever.
#
# Each entry is (label, compiled pattern). Patterns use a capturing group for
# the part to replace; if there is no group, the whole match is replaced.
REDACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # Provider-shaped tokens, matched on their own distinctive prefixes.
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("api-key", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}")),
    ("api-key", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("api-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"(?i)\b(?:bearer|authorization:\s*bearer)\s+([A-Za-z0-9._\-]{16,})")),
    # user:password@host in any URL-ish string.
    ("connection-string", re.compile(r"://[^\s/:@]+:([^\s/@\\]{3,})@")),
    # KEY=value / "key": "value" / key: value, for secret-ish key names.
    #
    # The name is matched as a *substring of an identifier*, not as a whole
    # word: real variables look like ``AGENTLOOM_DB_PASSWORD``, and ``\b`` never
    # fires before ``PASSWORD`` there because ``_`` is a word character. A
    # minimum value length keeps prose like "Password: see .env" intact.
    #
    # Backslashes are excluded from the value so that a JSON-escaped newline
    # after an empty assignment (the two characters ``\n``) is not mistaken for
    # a six-character secret.
    (
        "secret-assignment",
        re.compile(
            r"(?i)[\w.\-]*(?:password|passwd|secret|api[_-]?key|apikey|access[_-]?token"
            r"|auth[_-]?token|client[_-]?secret|private[_-]?token)[\w.\-]*"
            r"\s*[:=]\s*[\"']?([^\s\"',;)\\]{6,})"
        ),
    ),
)

REDACTION_PLACEHOLDER = "[redacted:{label}]"
_PLACEHOLDER_PREFIX = "[redacted:"


def redact(text: str) -> tuple[str, int]:
    """Strip credential-shaped substrings. Returns the text and a hit count.

    Conservative by construction: it removes things that *look* like secrets and
    leaves prose alone. It is a safety net for accidental echoes, not a licence
    to paste credentials into a conversation.

    Idempotent: re-running it over already-redacted text is a no-op. That makes
    "redact again and expect zero hits" a usable audit for whether the first
    pass was complete, which it would not be if placeholders re-matched.
    """
    if not text:
        return text, 0

    counter = [0]
    for label, pattern in REDACTION_PATTERNS:
        placeholder = REDACTION_PLACEHOLDER.format(label=label)

        def _sub(match: re.Match[str], _ph: str = placeholder) -> str:
            whole, offset = match.group(0), match.start()
            if match.groups() and match.span(1) != (-1, -1):
                start, end = match.span(1)
                value = whole[start - offset : end - offset]
                if value.startswith(_PLACEHOLDER_PREFIX):
                    return whole
                counter[0] += 1
                # Keep the surrounding context — the key name, the host — and
                # replace only the secret itself.
                return whole[: start - offset] + _ph + whole[end - offset :]
            if whole.startswith(_PLACEHOLDER_PREFIX):
                return whole
            counter[0] += 1
            return _ph

        text = pattern.sub(_sub, text)
    return text, counter[0]


@dataclass
class TranscriptBlock:
    """One piece of a turn: prose, or a tool invocation."""

    type: str  # "text" | "tool_use"
    text: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[str] = None  # rendered + redacted, not raw structure

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.text is not None:
            out["text"] = self.text
        if self.tool_name is not None:
            out["tool_name"] = self.tool_name
        if self.tool_input is not None:
            out["tool_input"] = self.tool_input
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptBlock":
        return cls(
            type=data.get("type", "text"),
            text=data.get("text"),
            tool_name=data.get("tool_name"),
            tool_input=data.get("tool_input"),
        )


@dataclass
class TranscriptTurn:
    seq: int
    role: str
    blocks: list[TranscriptBlock] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks if b.type == "text" and b.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "role": self.role,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptTurn":
        return cls(
            seq=int(data.get("seq", 0)),
            role=data.get("role", "agent"),
            blocks=[TranscriptBlock.from_dict(b) for b in data.get("blocks", [])],
        )


@dataclass
class TranscriptDocument:
    """A whole conversation, normalized and already redacted."""

    source_host: str
    source_ref: str
    turns: list[TranscriptTurn] = field(default_factory=list)
    redaction_count: int = 0

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def tail(self, limit: int) -> "TranscriptDocument":
        if limit <= 0 or limit >= len(self.turns):
            return self
        return TranscriptDocument(
            source_host=self.source_host,
            source_ref=self.source_ref,
            turns=self.turns[-limit:],
            redaction_count=self.redaction_count,
        )

    def around(self, seq: int, radius: int = 10) -> "TranscriptDocument":
        """Keep turns whose seq is within ``radius`` of ``seq`` (inclusive).

        Search hits return a seq range; this is how you page the archive at
        that location without loading the whole conversation into context.
        """
        if radius < 0:
            raise ValueError("radius must be >= 0")
        lo, hi = seq - radius, seq + radius
        turns = [t for t in self.turns if lo <= t.seq <= hi]
        return TranscriptDocument(
            source_host=self.source_host,
            source_ref=self.source_ref,
            turns=turns,
            redaction_count=self.redaction_count,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "source_host": self.source_host,
            "source_ref": self.source_ref,
            "redaction_count": self.redaction_count,
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptDocument":
        return cls(
            source_host=data.get("source_host", "unknown"),
            source_ref=data.get("source_ref", ""),
            turns=[TranscriptTurn.from_dict(t) for t in data.get("turns", [])],
            redaction_count=int(data.get("redaction_count", 0)),
        )


_ROLE_LABEL = {"human": "Human", "agent": "Agent", "system": "System"}


def _blocks_as_lines(turn: TranscriptTurn, tool_prefix: str) -> Iterable[str]:
    for block in turn.blocks:
        if block.type == "text" and block.text:
            yield block.text.strip()
        elif block.type == "tool_use":
            detail = f" {block.tool_input}" if block.tool_input else ""
            yield f"{tool_prefix}{block.tool_name or 'tool'}{detail}"


def render_text(doc: TranscriptDocument) -> str:
    """Plain-text rendering for a terminal or an agent's context window."""
    lines = [
        f"=== transcript {doc.source_ref} ({doc.source_host}) ===",
        f"{doc.turn_count} turn(s)"
        + (f", {doc.redaction_count} redaction(s)" if doc.redaction_count else ""),
        "",
    ]
    for turn in doc.turns:
        lines.append(f"--- [{turn.seq}] {_ROLE_LABEL.get(turn.role, turn.role)} ---")
        lines.extend(_blocks_as_lines(turn, tool_prefix="  · "))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(doc: TranscriptDocument) -> str:
    """Markdown rendering, for reading the conversation back in any editor."""
    lines = [
        f"# Transcript `{doc.source_ref}`",
        "",
        f"Captured from **{doc.source_host}** — {doc.turn_count} turn(s)"
        + (f", {doc.redaction_count} redaction(s)" if doc.redaction_count else "")
        + ".",
        "",
    ]
    for turn in doc.turns:
        lines.append(f"## [{turn.seq}] {_ROLE_LABEL.get(turn.role, turn.role)}")
        lines.append("")
        for block in turn.blocks:
            if block.type == "text" and block.text:
                lines.extend([block.text.strip(), ""])
            elif block.type == "tool_use":
                detail = f" — `{block.tool_input}`" if block.tool_input else ""
                lines.extend([f"> `{block.tool_name or 'tool'}`{detail}", ""])
    return "\n".join(lines).rstrip() + "\n"
