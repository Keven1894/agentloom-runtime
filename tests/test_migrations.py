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
        MIGRATIONS / "005_session_transcripts.sql",
        MIGRATIONS / "006_session_transcript_index.sql",
        MIGRATIONS / "007_session_lineage.sql",
        MIGRATIONS / "008_session_transcript_vectors.sql",
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


def test_session_index_migration_points_at_transcripts():
    sql = (MIGRATIONS / "006_session_transcript_index.sql").read_text(encoding="utf-8")
    assert "`session_transcript_chunks`" in sql
    assert "`session_transcripts`" in sql
    assert "ON DELETE CASCADE" in sql


def test_session_lineage_migration_adds_dag_columns():
    sql = (MIGRATIONS / "007_session_lineage.sql").read_text(encoding="utf-8")
    assert "`parent_session_id`" in sql
    assert "`fork_checkpoint_id`" in sql
    assert "`fork_reason`" in sql
    assert "`fk_agent_sessions_parent`" in sql
    assert "`fk_agent_sessions_fork_checkpoint`" in sql
    assert "ON DELETE SET NULL" in sql


def test_vector_migration_adds_compact_columns_without_dropping_the_old_one():
    """008 must be additive.

    A reader that has not been upgraded still reads the JSON column, so the
    migration can be applied before the code that supersedes it. Dropping the
    old column belongs to a later migration, after the backfill is verified.
    """
    sql = (MIGRATIONS / "008_session_transcript_vectors.sql").read_text(encoding="utf-8")
    assert "`embedding_f32`" in sql
    assert "`embedding_dim`" in sql
    assert "MEDIUMBLOB" in sql
    assert "DROP COLUMN" not in sql.upper()
