"""Smoke tests for bundled MySQL migrations."""

from __future__ import annotations

from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "mysql"


def test_core_migration_files_exist():
    files = [
        MIGRATIONS / "001_core_schema.sql",
        MIGRATIONS / "002_memory_embeddings.sql",
        MIGRATIONS / "003_kg_graph.sql",
        MIGRATIONS / "004_session_memory.sql",
    ]
    for path in files:
        assert path.is_file(), path.name


def test_session_migration_is_standalone():
    """Layer 0 must be appliable without the CORE schema."""
    session = (MIGRATIONS / "004_session_memory.sql").read_text(encoding="utf-8")
    for table in ("agent_sessions", "session_checkpoints", "session_turns"):
        assert f"`{table}`" in session
    # Its only foreign keys point at its own tables.
    for line in session.splitlines():
        if "REFERENCES" in line:
            assert "`agent_sessions`" in line, line.strip()


def test_core_migration_covers_runtime_tables():
    core = (MIGRATIONS / "001_core_schema.sql").read_text(encoding="utf-8")
    memory = (MIGRATIONS / "002_memory_embeddings.sql").read_text(encoding="utf-8")
    graph = (MIGRATIONS / "003_kg_graph.sql").read_text(encoding="utf-8")
    for table in ("messages", "tasks", "projects", "knowledge_embeddings"):
        assert f"`{table}`" in core
    for table in ("message_embeddings", "docshare_embeddings", "plan_embeddings"):
        assert f"`{table}`" in memory
    for table in ("kg_nodes", "kg_edges"):
        assert table in graph
