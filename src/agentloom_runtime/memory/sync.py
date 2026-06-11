"""Invalidate in-process embedding caches when rebuild markers update."""

from __future__ import annotations

import json
from pathlib import Path

_SYNC_MARKERS: dict[str, Path] = {}


def register_embedding_sync_marker(kind: str, marker_path: Path | str) -> None:
    """Register a filesystem marker used to detect embedding rebuilds."""
    _SYNC_MARKERS[kind] = Path(marker_path)


def clear_embedding_sync_markers() -> None:
    """Remove all registered sync markers (mainly for tests)."""
    _SYNC_MARKERS.clear()


def embeddings_sync_mtime(kind: str) -> float | None:
    path = _SYNC_MARKERS.get(kind)
    if path is None or not path.is_file():
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def embeddings_sync_timestamp(kind: str) -> str | None:
    path = _SYNC_MARKERS.get(kind)
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("timestamp")
    except Exception:
        return None
