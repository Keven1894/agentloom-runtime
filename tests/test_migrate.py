"""Schema installation: classification, statement splitting, and the ledger.

No database required. These cover the parts that are wrong *silently* — a
migration nobody classified, a statement split in the middle of a string
literal, a checksum that reports every file as modified on a CRLF checkout.
"""

from __future__ import annotations

from agentloom_runtime.db import migrate


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def test_every_migration_on_disk_is_classified():
    """A new .sql file that nobody added to MIGRATIONS would never be applied.

    `init` iterates MIGRATIONS, not the directory, so an unclassified file is
    invisible: no error, no record, and the missing table surfaces much later
    as a query failure.
    """
    on_disk = {p.name for p in migrate.migrations_dir().glob("*.sql")}
    classified = {m.filename for m in migrate.MIGRATIONS}
    assert on_disk == classified


def test_migrations_are_listed_in_application_order():
    names = [m.filename for m in migrate.MIGRATIONS]
    assert names == sorted(names)


def test_every_migration_belongs_to_a_known_group():
    for m in migrate.MIGRATIONS:
        assert m.group in migrate.GROUPS, m.filename


def test_session_group_is_installable_without_core():
    """The reference deployment runs Layer 0 with no CORE tables at all."""
    session = [m.filename for m in migrate.MIGRATIONS if m.group == "session"]
    assert session[0] == "004_session_memory.sql"
    assert "001_core_schema.sql" not in session


# --------------------------------------------------------------------------
# statement splitting
# --------------------------------------------------------------------------


def test_splitter_separates_plain_statements():
    assert migrate.split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_splitter_ignores_a_semicolon_inside_a_string_literal():
    sql = "INSERT INTO t (c) VALUES ('a;b'); SELECT 2;"
    assert migrate.split_statements(sql) == [
        "INSERT INTO t (c) VALUES ('a;b')",
        "SELECT 2",
    ]


def test_splitter_ignores_a_semicolon_inside_a_comment():
    sql = "-- drop everything; really\nSELECT 1;\n/* also; here */\nSELECT 2;"
    assert migrate.split_statements(sql) == ["SELECT 1", "SELECT 2"]


def test_splitter_keeps_backtick_identifiers_intact():
    sql = "CREATE TABLE `we;ird` (`a;b` INT);"
    assert migrate.split_statements(sql) == ["CREATE TABLE `we;ird` (`a;b` INT)"]


def test_splitter_tolerates_a_missing_trailing_semicolon():
    assert migrate.split_statements("SELECT 1") == ["SELECT 1"]


def test_splitter_emits_nothing_for_a_comment_only_file():
    assert migrate.split_statements("-- nothing to do\n\n") == []


def test_every_bundled_migration_splits_into_runnable_statements():
    for m in migrate.MIGRATIONS:
        text = (migrate.migrations_dir() / m.filename).read_text(encoding="utf-8")
        statements = migrate.split_statements(text)
        assert statements, m.filename
        for statement in statements:
            assert ";" not in statement.split("'")[0], (m.filename, statement[:60])


def test_no_migration_uses_index_syntax_mysql_rejects():
    """MySQL has no ``CREATE INDEX IF NOT EXISTS`` — that is MariaDB.

    All three CORE migrations shipped with it, so the CORE schema could not be
    installed on MySQL at all. Nothing caught it because the files were applied
    by hand, one at a time, by people who had already applied them.
    """
    for m in migrate.MIGRATIONS:
        text = (migrate.migrations_dir() / m.filename).read_text(encoding="utf-8")
        for statement in migrate.split_statements(text):
            assert "INDEX IF NOT EXISTS" not in statement.upper(), (m.filename, statement[:70])


# --------------------------------------------------------------------------
# checksum
# --------------------------------------------------------------------------


def test_checksum_ignores_line_ending_differences():
    """Otherwise every migration reads as modified on a CRLF checkout."""
    assert migrate._checksum("a\nb\n") == migrate._checksum("a\r\nb\r\n")


def test_checksum_detects_an_edited_migration():
    assert migrate._checksum("CREATE TABLE t (a INT)") != migrate._checksum(
        "CREATE TABLE t (a INT, b INT)"
    )


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def test_doctor_never_prints_the_database_password(monkeypatch, capsys):
    """Diagnostics get pasted into issues and chat logs.

    Reporting the target is the point; reporting the credential would make the
    command unsafe to run in the one situation it exists for.
    """
    import argparse

    from agentloom_runtime.session import cli

    secret = "hunter2-do-not-print"
    monkeypatch.setenv("AGENTLOOM_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENTLOOM_DB_PORT", "1")  # refused fast; connectivity is not the subject
    monkeypatch.setenv("AGENTLOOM_DB_NAME", "nope")
    monkeypatch.setenv("AGENTLOOM_DB_USER", "someone")
    monkeypatch.setenv("AGENTLOOM_DB_PASSWORD", secret)
    monkeypatch.setenv("AGENTLOOM_AGENT_ID", "test-agent")

    args = argparse.Namespace(
        agent=None, operator=None, workspace=None, path=None, json=False, group="session"
    )
    cli.cmd_doctor(args)

    out = capsys.readouterr()
    assert secret not in out.out
    assert secret not in out.err
    # The target itself must still be reported, or the check is useless.
    assert "someone@127.0.0.1:1/nope" in out.out
