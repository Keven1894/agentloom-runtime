"""Host-agnostic session identity.

A working session is identified by ``(agent_id, operator_id, workspace_key)``.

``workspace_key`` is derived from the repository's VCS remote, never from the
local filesystem path, the machine name, or the editor. That is what allows the
same session to be resumed from a different machine, a different checkout
directory, or a different IDE.

Everything in :class:`HostContext` is *provenance only*. It is recorded on
sessions and checkpoints so a human can see where work happened, and it must
never be used as a lookup key.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "HostContext",
    "detect_host_context",
    "detect_workspace_key",
    "normalize_workspace_key",
    "resolve_operator_id",
]

_SCP_LIKE = re.compile(r"^(?P<user>[^@/]+)@(?P<host>[^:/]+):(?P<path>.+)$")
_SCHEME = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://")


def normalize_workspace_key(remote_url: str) -> str:
    """Normalize a VCS remote URL into a stable, transport-independent key.

    All of these collapse to ``github.com/acme/widget``::

        git@github.com:Acme/widget.git
        https://github.com/Acme/widget
        ssh://git@github.com:22/Acme/widget.git

    The result is lowercased in full. Hosting services are effectively
    case-insensitive, and matching leniently is better than silently failing to
    resume because two machines cloned with different capitalization.
    """
    url = (remote_url or "").strip()
    if not url:
        raise ValueError("remote_url is empty")

    # Non-URL local fallbacks are passed through untouched.
    if url.startswith("local:"):
        return url.lower()

    scheme_match = _SCHEME.match(url)
    if scheme_match:
        rest = url[scheme_match.end() :]
        # Drop any userinfo (git@, oauth tokens, …).
        if "@" in rest.split("/", 1)[0]:
            rest = rest.split("@", 1)[1]
    else:
        scp = _SCP_LIKE.match(url)
        if scp:
            rest = f"{scp.group('host')}/{scp.group('path')}"
        else:
            rest = url

    # Strip an explicit port: host:22/path -> host/path
    head, sep, tail = rest.partition("/")
    if ":" in head:
        host, _, port = head.partition(":")
        if port.isdigit():
            head = host
    rest = f"{head}{sep}{tail}"

    rest = rest.strip("/")
    if rest.endswith(".git"):
        rest = rest[: -len(".git")]

    return rest.lower()


def _git(args: list[str], cwd: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def detect_workspace_key(path: Optional[Path] = None, remote: str = "origin") -> str:
    """Derive the workspace key for a checkout.

    Falls back to ``local:<directory-name>`` when the directory is not a VCS
    checkout or has no remote. That fallback still matches across machines when
    the directory name matches, but VCS remotes are strongly preferred.
    """
    override = os.environ.get("AGENTLOOM_WORKSPACE_KEY")
    if override:
        return normalize_workspace_key(override)

    root = Path(path or Path.cwd()).resolve()
    url = _git(["remote", "get-url", remote], root)
    if url:
        return normalize_workspace_key(url)

    toplevel = _git(["rev-parse", "--show-toplevel"], root)
    name = Path(toplevel).name if toplevel else root.name
    return f"local:{name}".lower()


def resolve_operator_id(explicit: Optional[str] = None) -> str:
    """Resolve the human operator, preferring an explicit or configured value."""
    for candidate in (explicit, os.environ.get("AGENTLOOM_OPERATOR_ID")):
        if candidate and candidate.strip():
            return candidate.strip()
    for var in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return "unknown"


@dataclass(frozen=True)
class HostContext:
    """Where work physically happened. Provenance only — never a lookup key."""

    host_hint: str
    ide_hint: str
    workspace_path_hint: str


def _detect_ide() -> str:
    """Best-effort editor label.

    Deliberately tolerant: an unrecognized host reports ``unknown`` and
    everything still works. Set ``AGENTLOOM_IDE`` to label a host explicitly.
    """
    explicit = os.environ.get("AGENTLOOM_IDE")
    if explicit and explicit.strip():
        return explicit.strip()[:64]

    env = os.environ
    if env.get("CURSOR_TRACE_ID") or env.get("CURSOR_AGENT"):
        return "cursor"
    term_program = (env.get("TERM_PROGRAM") or "").lower()
    if term_program:
        return term_program[:64]
    if env.get("CLINE_ACTIVE"):
        return "cline"
    if env.get("VSCODE_PID") or env.get("VSCODE_GIT_IPC_HANDLE"):
        return "vscode"
    return "unknown"


def detect_host_context(path: Optional[Path] = None) -> HostContext:
    root = Path(path or Path.cwd()).resolve()
    return HostContext(
        host_hint=platform.node()[:256] or "unknown",
        ide_hint=_detect_ide(),
        workspace_path_hint=str(root)[:1024],
    )
