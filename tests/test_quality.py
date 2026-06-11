"""Tests for agentloom_runtime.quality."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloom_runtime.quality import (
    KGIntegrityValidator,
    SchemaTarget,
    run_health_check,
    validate_kg_schema,
)


def test_validate_kg_schema_missing_file(tmp_path: Path):
    target = SchemaTarget(
        "demo",
        tmp_path / "missing.json",
        tmp_path / "schema.json",
    )
    ok, messages = validate_kg_schema(target)
    assert ok is False
    assert any("missing" in message.lower() for message in messages)


def test_kg_integrity_validator_detects_duplicate_ids(tmp_path: Path):
    kg_path = tmp_path / "builder-knowledge-graph.json"
    kg_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "dup",
                        "type": "concept",
                        "data": {"label": "A"},
                        "relationships": {"parent": "root", "children": []},
                    },
                    {
                        "id": "dup",
                        "type": "concept",
                        "data": {"label": "B"},
                        "relationships": {"parent": "root", "children": []},
                    },
                ],
                "metadata": {"total_nodes": 2},
            }
        ),
        encoding="utf-8",
    )
    passed, report = KGIntegrityValidator(kg_path, role="builder").validate_all()
    assert passed is False
    assert any("Duplicate ID" in error for error in report.errors)


def test_run_health_check_on_empty_kg_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTLOOM_REPO_ROOT", str(tmp_path))
    report = run_health_check(kg_dir=tmp_path / "agents" / "knowledge-graphs")
    assert report.overall_status == "FAIL"
    assert report.checks["schema_validation"]["failed"] > 0
