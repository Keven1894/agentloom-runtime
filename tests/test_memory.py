"""Tests for agentloom_runtime.memory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloom_runtime.memory import (
    clear_embedding_sync_markers,
    embeddings_sync_mtime,
    embeddings_sync_timestamp,
    reciprocal_rank_fusion,
    register_embedding_sync_marker,
    retrieval_use_rrf,
)


def test_reciprocal_rank_fusion_boosts_items_in_both_lists():
    list_a = [
        ("a", {"store": "kg"}),
        ("b", {"store": "kg"}),
        ("c", {"store": "kg"}),
    ]
    list_b = [
        ("b", {"store": "docshare"}),
        ("d", {"store": "docshare"}),
    ]
    fused = reciprocal_rank_fusion([list_a, list_b], rrf_k=60)
    keys = [item[0] for item in fused]
    assert keys[0] == "b"
    assert set(keys) == {"a", "b", "c", "d"}


def test_reciprocal_rank_fusion_respects_rrf_k():
    list_a = [("only", {"store": "kg"})]
    fused_k1 = reciprocal_rank_fusion([list_a], rrf_k=1)
    fused_k60 = reciprocal_rank_fusion([list_a], rrf_k=60)
    assert fused_k1[0][1] == pytest.approx(0.5)
    assert fused_k60[0][1] == pytest.approx(1.0 / 61.0)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("", False),
    ],
)
def test_retrieval_use_rrf(monkeypatch, value, expected):
    monkeypatch.setenv("AGENTLOOM_RETRIEVAL_USE_RRF", value)
    assert retrieval_use_rrf() is expected


def test_embedding_sync_markers(tmp_path: Path):
    clear_embedding_sync_markers()
    marker = tmp_path / "last_run.json"
    marker.write_text(json.dumps({"timestamp": "2026-06-11T12:00:00Z"}), encoding="utf-8")

    register_embedding_sync_marker("kg", marker)
    assert embeddings_sync_mtime("kg") is not None
    assert embeddings_sync_timestamp("kg") == "2026-06-11T12:00:00Z"
    assert embeddings_sync_mtime("unknown") is None

    clear_embedding_sync_markers()
    assert embeddings_sync_mtime("kg") is None
