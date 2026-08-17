"""Generate every AI coding host's rule file from one canonical source.

Keeps the bootstrap instruction identical across hosts without privileging any
of them: a target is a path plus optional front matter, and the emitter knows
about no specific editor.
"""

from agentloom_runtime.hostrules.emit import (
    Manifest,
    Target,
    TargetStatus,
    load_manifest,
    render,
    status,
    sync,
)

__all__ = [
    "Manifest",
    "Target",
    "TargetStatus",
    "load_manifest",
    "render",
    "status",
    "sync",
]
