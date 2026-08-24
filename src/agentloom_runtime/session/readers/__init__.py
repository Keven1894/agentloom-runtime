"""Host transcript readers.

Register a new host by adding a module here and listing its reader in
:data:`READERS`. Readers are read-only by contract; see
:mod:`agentloom_runtime.session.readers.base`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from agentloom_runtime.session.readers.base import TranscriptReader, TranscriptSource
from agentloom_runtime.session.readers.claude_code import ClaudeCodeTranscriptReader
from agentloom_runtime.session.readers.cursor import CursorTranscriptReader

__all__ = [
    "READERS",
    "TranscriptReader",
    "TranscriptSource",
    "discover_transcripts",
    "get_reader",
]

READERS: tuple[TranscriptReader, ...] = (
    CursorTranscriptReader(),
    ClaudeCodeTranscriptReader(),
)


def get_reader(host: str) -> Optional[TranscriptReader]:
    for reader in READERS:
        if reader.host == host:
            return reader
    return None


def discover_transcripts(
    workspace_path: Path,
    host: Optional[str] = None,
) -> list[TranscriptSource]:
    """Find every recorded conversation for a checkout, newest first.

    A reader that fails is skipped rather than fatal: one uncooperative host
    must not stop the others from being archived.
    """
    found: list[TranscriptSource] = []
    for reader in READERS:
        if host and reader.host != host:
            continue
        try:
            found.extend(reader.discover(Path(workspace_path)))
        except Exception:  # noqa: BLE001 - a broken reader is not a fatal error
            continue
    found.sort(key=lambda s: s.modified_at, reverse=True)
    return found
