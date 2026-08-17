"""Working-tree state captured with a checkpoint.

Summaries are short and redacted. A checkpoint records *that* a sensitive file
was dirty, never its name-adjacent contents and never a diff.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = ["VcsState", "collect_vcs_state"]

# Paths withheld from checkpoint summaries. Two rules, because a secret can be
# identified either by the directory holding it or by the file itself.
_SENSITIVE_DIR = re.compile(r"(^|/)(secrets?|credentials?|\.ssh|\.gnupg)/", re.IGNORECASE)
_SENSITIVE_FILE = re.compile(
    r"(^|/)(\.env(\..+)?|credentials?\.(json|ya?ml)|id_rsa|id_ed25519|"
    r"[^/]+\.(pem|key|p12|pfx|keystore|jks))$",
    re.IGNORECASE,
)


def _is_sensitive(path: str) -> bool:
    return bool(_SENSITIVE_DIR.search(path) or _SENSITIVE_FILE.search(path))

MAX_STATUS_LINES = 40


@dataclass
class VcsState:
    head: Optional[str] = None
    branch: Optional[str] = None
    status_summary: Optional[str] = None


def _git(args: list[str], cwd: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n") or None


def _summarize_status(porcelain: str) -> str:
    kept: list[str] = []
    redacted = 0
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) > 3 else line.strip()
        # Renames are reported as "old -> new"; check the destination.
        path = path.split(" -> ")[-1].strip().strip('"')
        if _is_sensitive(path):
            redacted += 1
            continue
        kept.append(line)

    overflow = max(0, len(kept) - MAX_STATUS_LINES)
    lines = kept[:MAX_STATUS_LINES]
    if overflow:
        lines.append(f"... and {overflow} more changed path(s)")
    if redacted:
        lines.append(f"[{redacted} sensitive path(s) withheld]")
    return "\n".join(lines)


def collect_vcs_state(path: Optional[Path] = None) -> VcsState:
    """Collect branch, HEAD, and a redacted working-tree summary."""
    root = Path(path or Path.cwd()).resolve()
    head = _git(["rev-parse", "HEAD"], root)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    porcelain = _git(["status", "--porcelain"], root)
    summary = _summarize_status(porcelain) if porcelain else "clean"
    return VcsState(head=head, branch=branch, status_summary=summary)
