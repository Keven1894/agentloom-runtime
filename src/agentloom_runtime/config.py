"""Environment discovery for AgentLoom Runtime.

One loader, used by every module that needs configuration. Before this existed
the database adapter and the embedding provider each had their own rules, which
is how an index could be fully embedded while every query silently fell back to
lexical-only: the adapter found the ``.env`` file and the embedding provider did
not.

Precedence never changes: a variable already present in the process environment
wins. A ``.env`` file only fills gaps, so CI and container deployments that
inject real environment variables are unaffected by a stray file on disk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

__all__ = ["find_env_file", "load_env"]

# How far up from the working directory to look. A repository checkout nested a
# few levels deep still finds its root; an unrelated file far up the tree does
# not get picked up by accident.
_MAX_PARENTS = 5

_loaded: set[str] = set()


def find_env_file(start: Optional[Path] = None) -> Optional[Path]:
    """Locate the ``.env`` that applies here.

    ``AGENTLOOM_ENV_FILE`` is authoritative when set — that is how a service or
    a scheduled task pins configuration independent of its working directory.
    Otherwise walk up from ``start`` (default: the working directory).
    """
    explicit = os.environ.get("AGENTLOOM_ENV_FILE")
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None

    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents][:_MAX_PARENTS + 1]:
        env_path = candidate / ".env"
        if env_path.is_file():
            return env_path
    return None


def load_env(start: Optional[Path] = None, force: bool = False) -> Optional[Path]:
    """Fill missing environment variables from the applicable ``.env``.

    Returns the file used, or ``None`` when there was none. Loading the same
    file twice is a no-op unless ``force`` is set, so calling this from every
    entry point costs nothing.
    """
    env_path = find_env_file(start)
    if env_path is None:
        return None

    key = str(env_path)
    if key in _loaded and not force:
        return env_path

    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or name.startswith("export "):
            name = name.removeprefix("export ").strip()
        # setdefault, never assignment: the real environment always wins.
        os.environ.setdefault(name, value.strip().strip('"').strip("'"))

    _loaded.add(key)
    return env_path
