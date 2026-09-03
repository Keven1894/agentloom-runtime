"""Smoke tests for bundled MySQL migrations."""

from __future__ import annotations

from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "mysql"


def _executable(name: str) -> str:
    """A migration's statements with its commentary stripped.

    These files carry long rationale headers, so asserting against the raw text
    makes a test pass or fail on prose. What a migration *does* is the part
    worth pinning.
    """
    text = (MIGRATIONS / name).read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )


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
        MIGRATIONS / "010_session_transcript_titles.sql",
        MIGRATIONS / "011_session_transcript_title_source.sql",
        MIGRATIONS / "012_session_transcript_captured_at_is_host_mtime.sql",
        MIGRATIONS / "013_session_transcript_presentation.sql",
        MIGRATIONS / "014_session_transcript_chunk_locale.sql",
        MIGRATIONS / "015_session_job_trace.sql",
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


def test_transcript_title_arrives_as_its_own_migration():
    """A shipped migration must never be edited in place.

    `005` creates the table with `IF NOT EXISTS`, so re-running it against a
    deployment that already applied it is a no-op. Adding `title` there would
    mean every existing install silently lacks the column and every listing
    query fails on `Unknown column 'title'`. New columns get a new file.
    """
    create = (MIGRATIONS / "005_session_transcripts.sql").read_text(encoding="utf-8")
    assert "`title`" not in create

    alter = (MIGRATIONS / "010_session_transcript_titles.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE `session_transcripts`" in alter
    assert "ADD COLUMN `title`" in alter
    assert "DROP" not in alter.upper()


def test_transcript_title_source_arrives_as_its_own_migration():
    """A user rename must survive the next archive write.

    010 only stored the derived heading. Putting the lock in that same file
    would leave existing installs without the column, and the next `archive`
    would keep clobbering names people just typed.
    """
    sql = (MIGRATIONS / "011_session_transcript_title_source.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN `title_source`" in sql
    assert "derived" in sql
    assert "user" in sql
    assert "DROP" not in sql.upper()


def test_captured_at_loses_on_update_current_timestamp():
    """The listing date is the conversation, not the ingest.

    ON UPDATE CURRENT_TIMESTAMP on captured_at made a bulk archive stamp
    every row with the same second. 012 removes the trigger; the application
    writes host file mtime instead.
    """
    original = (MIGRATIONS / "005_session_transcripts.sql").read_text(encoding="utf-8")
    assert "ON UPDATE CURRENT_TIMESTAMP" in original
    later = (MIGRATIONS / "012_session_transcript_captured_at_is_host_mtime.sql").read_text(
        encoding="utf-8"
    )
    assert "MODIFY COLUMN `captured_at`" in later
    modify = [line for line in later.splitlines() if "MODIFY COLUMN" in line]
    assert modify and "ON UPDATE" not in modify[0].upper()


def test_transcript_presentation_arrives_as_its_own_migration():
    """Listing copy is optional JSON; the archive body stays the source of truth."""
    sql = (MIGRATIONS / "013_session_transcript_presentation.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN `presentation_json`" in sql
    assert "JSON" in sql
    assert "DROP" not in sql.upper()


def test_chunk_locale_arrives_as_its_own_migration():
    """Translated overlays must not collide with the original unique key."""
    sql = (MIGRATIONS / "014_session_transcript_chunk_locale.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN `locale`" in sql
    assert "DROP INDEX `uq_session_transcript_chunks`" in sql
    assert "DROP COLUMN" not in sql.upper()


def test_job_trace_keys_items_by_work_kind_not_by_run():
    """Resume must survive a machine change, so the ledger cannot be per-run.

    Keying per-transcript state on `(job_kind, transcript_id)` is what lets a
    restart anywhere read back the same completion set. Keying it on the run
    would make every restart a fresh queue.
    """
    sql = (MIGRATIONS / "015_session_job_trace.sql").read_text(encoding="utf-8")
    assert "PRIMARY KEY (`job_kind`, `transcript_id`)" in sql
    for table in ("session_job_runs", "session_job_items", "session_job_events"):
        assert f"`{table}`" in sql


def test_job_trace_keeps_the_review_verdict_and_the_body_fingerprint():
    """The verdict is the part that cannot be recomputed for free.

    `body_sha256` sits next to it so an edited transcript re-enters the queue
    instead of inheriting a pass granted to its earlier text.
    """
    sql = (MIGRATIONS / "015_session_job_trace.sql").read_text(encoding="utf-8")
    verdict = [line for line in sql.splitlines() if "`qc_report_json`" in line]
    assert verdict and "JSON" in verdict[0]
    assert "`body_sha256`" in sql
    assert "`qc_score`" in sql


def test_job_events_are_append_only_and_ordered_within_a_run():
    sql = (MIGRATIONS / "015_session_job_trace.sql").read_text(encoding="utf-8")
    assert "UNIQUE KEY `uq_session_job_events_seq` (`run_id`, `seq`)" in sql
    assert "AUTO_INCREMENT" in sql
    assert "DROP" not in sql.upper()


def test_lane_arrives_without_touching_the_open_session_key():
    """016 must be safe to apply while another host still runs pre-lane code.

    Adding the column and changing `open_key` in one file would let a second
    lane open while an un-upgraded host is still resolving sessions without a
    lane filter -- and that host's implicit open, the one `checkpoint` does,
    would then file its checkpoint under whichever lane the optimizer returned.
    """
    sql = _executable("016_session_lanes.sql")
    assert "ADD COLUMN `lane`" in sql
    assert "DEFAULT 'default'" in sql
    assert "`session_hosts`" in sql
    assert "open_key" not in sql
    assert "DROP" not in sql.upper()


def test_lane_identity_rebuilds_the_generated_key():
    """MySQL cannot redefine a stored generated column an index depends on, so
    017 drops both and rebuilds them. The lane must end up inside the hash."""
    sql = _executable("017_session_lane_identity.sql")
    assert "DROP INDEX `uq_agent_sessions_open`" in sql
    assert "DROP COLUMN `open_key`" in sql
    assert "ADD UNIQUE KEY `uq_agent_sessions_open`" in sql
    # Same four-part identity the code resolves.
    hashed = sql[sql.index("SHA2") : sql.index("STORED")]
    for part in ("`agent_id`", "`operator_id`", "`workspace_key`", "`lane`"):
        assert part in hashed


def test_session_hosts_records_activity_without_becoming_a_lookup_key():
    """The table is keyed by machine because "which machine" is its question.

    Session lookup must still never key on a host, so this table carries its
    own columns rather than reusing the provenance hint names.
    """
    sql = _executable("016_session_lanes.sql")
    hosts = sql[sql.index("CREATE TABLE IF NOT EXISTS `session_hosts`") :]
    assert "PRIMARY KEY (`session_id`, `host`)" in hosts
    assert "host_hint" not in hosts
    assert "ON DELETE CASCADE" in hosts


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
