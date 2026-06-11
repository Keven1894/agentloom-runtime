"""Configurable paths for KG authoring tree and sync artifacts."""

from __future__ import annotations

import os
from pathlib import Path


def get_repo_root() -> Path:
    override = os.environ.get("AGENTLOOM_REPO_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    return Path.cwd().resolve()


def get_kg_dir() -> Path:
    override = os.environ.get("AGENTLOOM_KG_DIR", "").strip()
    if override:
        return Path(override).resolve()
    return get_repo_root() / "agents" / "knowledge-graphs"


def get_sync_report_path() -> Path:
    override = os.environ.get("AGENTLOOM_KG_SYNC_REPORT", "").strip()
    if override:
        return Path(override).resolve()
    return get_repo_root() / ".agentloom" / "kg_sync" / "last_run.json"
