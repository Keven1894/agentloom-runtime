"""Tests for agentloom_runtime.kg."""

from __future__ import annotations

import os
from pathlib import Path

from agentloom_runtime.kg import format_for_prompt, get_kg_dir, get_repo_root, search_kg
from agentloom_runtime.kg.paths import get_sync_report_path


def test_paths_from_env(monkeypatch, tmp_path: Path):
    repo = tmp_path / "agent"
    kg = repo / "agents" / "knowledge-graphs"
    kg.mkdir(parents=True)
    monkeypatch.setenv("AGENTLOOM_REPO_ROOT", str(repo))
    monkeypatch.delenv("AGENTLOOM_KG_DIR", raising=False)
    assert get_repo_root() == repo.resolve()
    assert get_kg_dir() == kg.resolve()
    assert get_sync_report_path() == (repo / ".agentloom" / "kg_sync" / "last_run.json").resolve()


def test_search_kg_empty_query():
    assert search_kg("") == []
    assert search_kg("   ") == []


def test_format_for_prompt_empty():
    assert format_for_prompt([]) == "(no relevant knowledge found)"


def test_format_for_prompt_truncates():
    results = [
        {
            "node_type": "skill",
            "topic": "Test skill",
            "score": 0.9,
            "content": "x" * 1000,
        }
    ]
    text = format_for_prompt(results, max_chars=100)
    assert len(text) <= 100
