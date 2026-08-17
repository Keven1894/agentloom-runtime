"""The plugin boundary for host transcript readers.

Session *identity* can be host-neutral, and rule-file *emission* can be
host-neutral, but conversation *capture* cannot: each host writes its own
format in its own location. Rather than pretend otherwise, this is an explicit
plugin seam. Supporting a new host means adding one module here; nothing
outside :mod:`agentloom_runtime.session.readers` changes.

Readers are strictly **read-only**. They open files a host already wrote. No
reader may write to, lock, or migrate a host's own storage — that is how you
corrupt someone's editor, and it is why the design does not attempt to restore
a conversation into a host's native UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from agentloom_runtime.session.transcript import TranscriptDocument, redact

__all__ = [
    "MAX_TOOL_INPUT_CHARS",
    "MAX_TOOL_VALUE_CHARS",
    "TranscriptReader",
    "TranscriptSource",
    "summarize_tool_input",
]

# Tool inputs carry whole file bodies for write-style tools. Those bodies are
# already in version control, they are the likeliest place for a credential to
# sit, and they would dominate the archive. Keep the shape of the call — paths,
# commands, patterns — and cap the bulky values.
MAX_TOOL_VALUE_CHARS = 200
MAX_TOOL_INPUT_CHARS = 600


@dataclass(frozen=True)
class TranscriptSource:
    """A conversation a host has recorded, before it is parsed."""

    host: str
    ref: str
    path: Path
    modified_at: float
    size_bytes: int


@runtime_checkable
class TranscriptReader(Protocol):
    """Discover and parse the conversations one host records."""

    host: str

    def discover(self, workspace_path: Path) -> list[TranscriptSource]:
        """Return this host's transcripts for a checkout, newest first.

        Must return an empty list — never raise — when the host is not
        installed or has recorded nothing.
        """

    def read(self, source: TranscriptSource) -> TranscriptDocument:
        """Parse one transcript into the normalized, redacted form."""


def _render_value(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    if len(text) > MAX_TOOL_VALUE_CHARS:
        dropped = len(text) - MAX_TOOL_VALUE_CHARS
        text = f"{text[:MAX_TOOL_VALUE_CHARS]}… (+{dropped} chars)"
    return text


def summarize_tool_input(raw: Any) -> tuple[Optional[str], int]:
    """Render a tool invocation compactly and redact it.

    Per-field rather than whole-blob truncation, so short identifying fields
    like a path survive intact while a file body is cut down.

    Returns ``(summary, redaction_count)``. The count is returned rather than
    swallowed so a transcript's reported redaction total covers tool arguments
    too — under-reporting it would make the number worse than useless.
    """
    if raw is None:
        return None, 0

    if isinstance(raw, dict):
        summary = ", ".join(f"{key}={_render_value(value)}" for key, value in raw.items())
        if len(summary) > MAX_TOOL_INPUT_CHARS:
            summary = summary[:MAX_TOOL_INPUT_CHARS] + "…"
    else:
        summary = _render_value(raw)

    summary, hits = redact(summary)
    return (summary or None), hits
