"""Schema installation and version tracking.

Applying the schema used to mean pasting ten ``mysql < file.sql`` commands in
the right order, with the dependency rules living only in prose in the README.
That is the single biggest barrier to mounting this runtime on someone else's
deployment, and it has no safe failure mode: a missed file surfaces much later
as ``Unknown column`` from a query that looks unrelated.

Two ideas make it safe.

**Groups.** ``core`` (messages, tasks, projects, KG) and ``session`` (Layer 0
memory) are independent. A deployment that only wants cross-machine session
continuity installs ``session`` alone, which is the common case — the runtime's
own reference deployment runs the session group with no CORE tables at all.

**A ledger.** ``agentloom_schema_history`` records what has actually been
applied, keyed by filename and fingerprinted by checksum. This is the tool's own
bookkeeping table, created outside the numbered sequence, so it works no matter
which group you install — the same reason Flyway and Alembic own their history
tables rather than shipping them as migration 001.

The checksum is not decoration. Editing an already-shipped migration in place is
invisible to ``CREATE TABLE IF NOT EXISTS``: fresh installs get the change and
existing ones silently do not. Recording the checksum turns that into something
:func:`status` can report.

The legacy ``schema_version`` table created by ``001_core_schema.sql`` is a
hand-maintained human log. It is deliberately left alone; nothing here reads or
writes it.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from agentloom_runtime.db import connect

__all__ = [
    "GROUPS",
    "LEDGER_TABLE",
    "Migration",
    "MIGRATIONS",
    "MigrationState",
    "apply_migrations",
    "migrations_dir",
    "pending",
    "split_statements",
    "status",
]

LEDGER_TABLE = "agentloom_schema_history"

GROUPS = ("core", "session")


@dataclass(frozen=True)
class Migration:
    filename: str
    group: str
    summary: str


# Ordered exactly as they must be applied. The group is explicit rather than
# inferred from the number so that adding a file forces a deliberate choice;
# ``test_migrate`` fails if a file on disk is missing from this list.
MIGRATIONS: tuple[Migration, ...] = (
    Migration("001_core_schema.sql", "core", "messages, tasks, projects, embeddings"),
    Migration("002_memory_embeddings.sql", "core", "message/docshare/plan embeddings"),
    Migration("003_kg_graph.sql", "core", "knowledge-graph nodes and edges"),
    Migration("004_session_memory.sql", "session", "Layer 0 sessions, checkpoints, turns"),
    Migration("005_session_transcripts.sql", "session", "conversation archive"),
    Migration("006_session_transcript_index.sql", "session", "archive search index"),
    Migration("007_session_lineage.sql", "session", "session DAG topology"),
    Migration("008_session_transcript_vectors.sql", "session", "compact float32 embeddings"),
    Migration("009_drop_json_embeddings.sql", "session", "retire JSON embedding column"),
    Migration("010_session_transcript_titles.sql", "session", "human-readable titles"),
)


@dataclass(frozen=True)
class MigrationState:
    """One migration's position relative to a live database."""

    migration: Migration
    state: str  # "applied" | "pending" | "changed" | "missing"
    applied_at: Optional[str] = None
    baseline: bool = False


def migrations_dir() -> Path:
    """Locate the bundled SQL.

    ``AGENTLOOM_MIGRATIONS_DIR`` wins, which is how a packaged deployment points
    at files installed somewhere other than a source checkout. Otherwise look
    inside the package first, then fall back to the repository layout used by an
    editable install.
    """
    override = os.environ.get("AGENTLOOM_MIGRATIONS_DIR")
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    packaged = here.parent.parent / "migrations" / "mysql"
    if packaged.is_dir():
        return packaged
    return here.parents[2].parent / "migrations" / "mysql"


def split_statements(sql: str) -> list[str]:
    """Split a migration file into individual statements.

    PyMySQL executes one statement per call, so the file has to be split. Naive
    ``sql.split(";")`` is wrong the moment a semicolon appears inside a string
    literal or a comment, so track quoting and comment state instead. No
    migration may use ``DELIMITER``; stored programs are out of scope.
    """
    statements: list[str] = []
    buf: list[str] = []
    quote: Optional[str] = None
    line_comment = False
    block_comment = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if line_comment:
            if ch == "\n":
                line_comment = False
                buf.append(ch)
            i += 1
            continue

        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue

        if quote:
            buf.append(ch)
            if ch == "\\" and quote in ("'", '"'):
                if nxt:
                    buf.append(nxt)
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue

        if ch == "-" and nxt == "-":
            line_comment = True
            i += 2
            continue
        if ch == "#":
            line_comment = True
            i += 1
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _checksum(text: str) -> str:
    # Newlines are normalized so a checkout with different line endings does not
    # report every migration as modified.
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _read(migration: Migration) -> Optional[str]:
    path = migrations_dir() / migration.filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _selected(group: Optional[str]) -> tuple[Migration, ...]:
    if group is None:
        return MIGRATIONS
    if group not in GROUPS:
        raise ValueError(f"unknown group {group!r}; expected one of {', '.join(GROUPS)}")
    return tuple(m for m in MIGRATIONS if m.group == group)


def ensure_ledger(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{LEDGER_TABLE}` (
          `filename`   VARCHAR(255) NOT NULL,
          `group_name` VARCHAR(32)  NOT NULL,
          `checksum`   CHAR(64)     NOT NULL,
          `applied_at` DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
          -- Recorded without being executed, to adopt a hand-built database.
          `baseline`   TINYINT(1)   NOT NULL DEFAULT 0,
          PRIMARY KEY (`filename`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
        """
    )
    conn.commit()


def _ledger_rows(conn) -> dict[str, dict]:
    rows = conn.execute(
        f"SELECT filename, checksum, applied_at, baseline FROM `{LEDGER_TABLE}`"
    ).fetchall()
    return {
        row["filename"]: {
            "checksum": row["checksum"],
            "applied_at": row["applied_at"],
            "baseline": bool(row["baseline"]),
        }
        for row in rows
    }


def status(group: Optional[str] = None, conn=None) -> list[MigrationState]:
    """Report each migration's state against the live database."""
    own = conn is None
    conn = conn or connect()
    try:
        ensure_ledger(conn)
        recorded = _ledger_rows(conn)
        out: list[MigrationState] = []
        for migration in _selected(group):
            text = _read(migration)
            entry = recorded.get(migration.filename)
            if text is None:
                state = "missing"
            elif entry is None:
                state = "pending"
            elif entry["checksum"] != _checksum(text):
                state = "changed"
            else:
                state = "applied"
            out.append(
                MigrationState(
                    migration=migration,
                    state=state,
                    applied_at=str(entry["applied_at"]) if entry else None,
                    baseline=bool(entry["baseline"]) if entry else False,
                )
            )
        return out
    finally:
        if own:
            conn.close()


def pending(group: Optional[str] = None, conn=None) -> list[Migration]:
    return [s.migration for s in status(group, conn=conn) if s.state == "pending"]


def apply_migrations(
    group: Optional[str] = None,
    baseline: bool = False,
    conn=None,
) -> list[tuple[str, str]]:
    """Apply every pending migration in order. Returns ``(filename, action)``.

    ``baseline`` records the migrations as applied *without executing them*, to
    adopt a database whose tables were created by hand. That is a separate verb
    rather than an automatic fallback because guessing wrong in either direction
    is destructive: re-running an ``ALTER`` fails loudly, and skipping one that
    was never applied fails much later and quietly.

    Already-applied migrations are skipped, so this is safe to re-run.
    """
    own = conn is None
    conn = conn or connect()
    actions: list[tuple[str, str]] = []
    try:
        ensure_ledger(conn)
        recorded = _ledger_rows(conn)

        for migration in _selected(group):
            if migration.filename in recorded:
                actions.append((migration.filename, "skipped"))
                continue

            text = _read(migration)
            if text is None:
                raise FileNotFoundError(
                    f"{migration.filename} not found in {migrations_dir()}. "
                    "Set AGENTLOOM_MIGRATIONS_DIR if the SQL lives elsewhere."
                )

            if not baseline:
                for statement in split_statements(text):
                    conn.execute(statement)

            conn.execute(
                f"INSERT INTO `{LEDGER_TABLE}` (filename, group_name, checksum, baseline) "
                "VALUES (?, ?, ?, ?)",
                [migration.filename, migration.group, _checksum(text), 1 if baseline else 0],
            )
            conn.commit()
            actions.append((migration.filename, "recorded" if baseline else "applied"))
        return actions
    finally:
        if own:
            conn.close()


def describe(states: Iterable[MigrationState]) -> str:
    """Render migration state as an aligned, human-readable block."""
    marks = {"applied": "ok", "pending": "PENDING", "changed": "CHANGED", "missing": "MISSING"}
    lines = []
    for item in states:
        mark = marks.get(item.state, item.state)
        suffix = " (baseline)" if item.baseline else ""
        lines.append(
            f"  [{mark:>7}] {item.migration.filename:<38} "
            f"{item.migration.group:<7} {item.migration.summary}{suffix}"
        )
    return "\n".join(lines)
