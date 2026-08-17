"""Read-only reader for Cursor's agent transcripts.

Cursor records each conversation as JSONL at a path that is the same on
Windows, macOS, and Linux::

    ~/.cursor/projects/<workspace-slug>/agent-transcripts/<uuid>/<uuid>.jsonl

Lines are ``{"role": "user"|"assistant", "message": {"content": [...]}}`` with
content blocks of type ``text`` and ``tool_use``; tool *results* are not
recorded. Bookkeeping lines such as ``turn_ended`` are ignored.

This layout is not a published API. The reader therefore treats every step as
optional — a missing directory, an unfamiliar line, or a changed field yields
fewer turns rather than an exception — and it never writes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentloom_runtime.session.readers.base import TranscriptSource, summarize_tool_input
from agentloom_runtime.session.transcript import (
    TranscriptBlock,
    TranscriptDocument,
    TranscriptTurn,
    redact,
)

__all__ = ["CursorTranscriptReader", "workspace_slug"]

HOST = "cursor"

_ROLE_MAP = {"user": "human", "assistant": "agent", "system": "system"}
_NON_SLUG = re.compile(r"[^a-z0-9]+")

# Cursor wraps the real question in these; unwrapping keeps the rendered
# conversation readable instead of showing harness scaffolding.
_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)


def workspace_slug(workspace_path: Path) -> str:
    """Derive Cursor's project folder name from a checkout path.

    ``C:\\projects\\widget`` becomes ``c-projects-widget``.
    """
    return _NON_SLUG.sub("-", str(workspace_path).lower()).strip("-")


def _projects_root() -> Path:
    return Path.home() / ".cursor" / "projects"


class CursorTranscriptReader:
    host = HOST

    def __init__(self, projects_root: Path | None = None):
        self._root = projects_root or _projects_root()

    def discover(self, workspace_path: Path) -> list[TranscriptSource]:
        if not self._root.is_dir():
            return []

        slug = workspace_slug(Path(workspace_path).resolve())
        candidates = [self._root / slug]
        # The slug rule is inferred, not documented, so accept a folder whose
        # name merely ends with the checkout's own directory name.
        tail = Path(workspace_path).resolve().name.lower()
        candidates += [
            d
            for d in self._root.iterdir()
            if d.is_dir() and d.name != slug and d.name.endswith(tail)
        ]

        sources: list[TranscriptSource] = []
        for project_dir in candidates:
            transcripts = project_dir / "agent-transcripts"
            if not transcripts.is_dir():
                continue
            for jsonl in transcripts.glob("*/*.jsonl"):
                try:
                    stat = jsonl.stat()
                except OSError:
                    continue
                sources.append(
                    TranscriptSource(
                        host=HOST,
                        ref=jsonl.stem,
                        path=jsonl,
                        modified_at=stat.st_mtime,
                        size_bytes=stat.st_size,
                    )
                )

        sources.sort(key=lambda s: s.modified_at, reverse=True)
        return sources

    def read(self, source: TranscriptSource) -> TranscriptDocument:
        doc = TranscriptDocument(source_host=HOST, source_ref=source.ref)
        seq = 0
        with source.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue

                role = _ROLE_MAP.get(entry.get("role", ""))
                if role is None:
                    continue  # turn_ended and other bookkeeping

                blocks, redactions = self._parse_blocks(entry.get("message"), role)
                if not blocks:
                    continue

                seq += 1
                doc.turns.append(TranscriptTurn(seq=seq, role=role, blocks=blocks))
                doc.redaction_count += redactions
        return doc

    def _parse_blocks(self, message: Any, role: str) -> tuple[list[TranscriptBlock], int]:
        if not isinstance(message, dict):
            return [], 0
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return [], 0

        blocks: list[TranscriptBlock] = []
        redactions = 0
        for raw in content:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("type")

            if kind == "text":
                text = raw.get("text") or ""
                if role == "human":
                    match = _USER_QUERY.search(text)
                    if match:
                        text = match.group(1)
                text, hits = redact(text.strip())
                redactions += hits
                if text:
                    blocks.append(TranscriptBlock(type="text", text=text))

            elif kind == "tool_use":
                summary, hits = summarize_tool_input(raw.get("input"))
                redactions += hits
                blocks.append(
                    TranscriptBlock(
                        type="tool_use",
                        tool_name=raw.get("name"),
                        tool_input=summary,
                    )
                )

        return blocks, redactions
