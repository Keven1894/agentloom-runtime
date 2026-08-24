"""Read-only reader for Claude Code's session transcripts.

Claude Code records each session as JSONL under a per-directory folder::

    ~/.claude/projects/<cwd-with-separators-replaced>/<session-uuid>.jsonl

The folder name is the working directory with ``:``, ``\\`` and ``/`` each
replaced by ``-`` — ``C:\\Users\\dev`` becomes ``C--Users-dev``. Unlike Cursor's
slug, runs are not collapsed and case is preserved, so the two hosts need
different derivations even though the shape is otherwise similar.

Lines are heterogeneous. Only ``user``, ``assistant`` and ``system`` entries
carry a ``message``; the rest (``mode``, ``attachment``, ``file-history-snapshot``,
``ai-title``, …) are bookkeeping and are skipped, except ``ai-title``, whose
``aiTitle`` is the name Claude Code gave the conversation and is worth keeping.

Two kinds of content are deliberately dropped:

* ``thinking`` blocks — internal reasoning, large, and not what anyone re-reads.
* ``tool_result`` blocks — the same rule the rest of this package follows: tool
  results are bulky and the likeliest place for a credential to surface.

Sidechain entries (``isSidechain``) belong to sub-agent conversations that run
inside the session. Interleaving them into the main thread makes the transcript
unreadable, so they are skipped rather than merged.

Like every reader here this layout is not a published API, so each step is
optional — an unfamiliar line or a changed field yields fewer turns rather than
an exception — and it never writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agentloom_runtime.session.readers.base import TranscriptSource, summarize_tool_input
from agentloom_runtime.session.transcript import (
    TranscriptBlock,
    TranscriptDocument,
    TranscriptTurn,
    redact,
)

__all__ = ["ClaudeCodeTranscriptReader", "project_slug"]

HOST = "claude-code"

_ROLE_MAP = {"user": "human", "assistant": "agent", "system": "system"}
_SEPARATORS = (":", "\\", "/")


def project_slug(workspace_path: Path) -> str:
    """Derive Claude Code's project folder name from a checkout path.

    ``C:\\Users\\dev`` becomes ``C--Users-dev``; ``/home/dev/widget`` becomes
    ``-home-dev-widget``. Each separator maps to one dash, so consecutive
    separators produce consecutive dashes.
    """
    text = str(workspace_path)
    for sep in _SEPARATORS:
        text = text.replace(sep, "-")
    return text


def _projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


class ClaudeCodeTranscriptReader:
    host = HOST

    def __init__(self, projects_root: Path | None = None):
        self._root = projects_root or _projects_root()

    def discover(self, workspace_path: Path) -> list[TranscriptSource]:
        if not self._root.is_dir():
            return []

        resolved = Path(workspace_path).resolve()
        slug = project_slug(resolved)
        candidates = [self._root / slug]
        # The slug rule is inferred rather than documented, so also accept a
        # folder whose name merely ends with the checkout's own directory name.
        tail = resolved.name.lower()
        try:
            candidates += [
                d
                for d in self._root.iterdir()
                if d.is_dir() and d.name != slug and d.name.lower().endswith(tail)
            ]
        except OSError:
            return []

        sources: list[TranscriptSource] = []
        for project_dir in candidates:
            if not project_dir.is_dir():
                continue
            for jsonl in project_dir.glob("*.jsonl"):
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
                if not isinstance(entry, dict):
                    continue

                if entry.get("type") == "ai-title":
                    title = entry.get("aiTitle")
                    if isinstance(title, str) and title.strip():
                        doc.title_hint = title.strip()
                    continue

                if entry.get("isSidechain") or entry.get("isMeta"):
                    continue

                role = _ROLE_MAP.get(entry.get("type", ""))
                if role is None:
                    continue

                blocks, redactions = self._parse_blocks(entry.get("message"))
                if not blocks:
                    continue

                seq += 1
                doc.turns.append(TranscriptTurn(seq=seq, role=role, blocks=blocks))
                doc.redaction_count += redactions
        return doc

    def _parse_blocks(self, message: Any) -> tuple[list[TranscriptBlock], int]:
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
                text, hits = redact((raw.get("text") or "").strip())
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
