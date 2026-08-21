"""Lightweight MySQL database adapter for AgentLoom runtime code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from agentloom_runtime.config import load_env


@dataclass(frozen=True)
class DatabaseSettings:
    driver: str
    host: Optional[str] = None
    port: int = 3306
    database: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    charset: str = "utf8mb4"


def get_database_settings() -> DatabaseSettings:
    """Read MySQL settings from environment variables."""
    load_env()
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("AGENTLOOM_DATABASE_URL")
    if database_url:
        parsed = urlparse(database_url)
        scheme = parsed.scheme.lower()
        if scheme in {"mysql", "mysql+pymysql"}:
            query = parse_qs(parsed.query)
            return DatabaseSettings(
                driver="mysql",
                host=parsed.hostname,
                port=parsed.port or 3306,
                database=parsed.path.lstrip("/") or None,
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                charset=(query.get("charset") or ["utf8mb4"])[0],
            )
        if scheme == "sqlite":
            raise RuntimeError(
                "SQLite DATABASE_URL is not supported. Use mysql://… or AGENTLOOM_DB_* variables."
            )

    driver = os.environ.get("AGENTLOOM_DB_DRIVER", "mysql").lower()
    if driver == "sqlite":
        raise RuntimeError(
            "AGENTLOOM_DB_DRIVER=sqlite is not supported. Set AGENTLOOM_DB_DRIVER=mysql "
            "and AGENTLOOM_DB_HOST/NAME/USER."
        )
    return DatabaseSettings(
        driver="mysql",
        host=os.environ.get("AGENTLOOM_DB_HOST"),
        port=int(os.environ.get("AGENTLOOM_DB_PORT", "3306")),
        database=os.environ.get("AGENTLOOM_DB_NAME"),
        user=os.environ.get("AGENTLOOM_DB_USER"),
        password=os.environ.get("AGENTLOOM_DB_PASSWORD"),
        charset=os.environ.get("AGENTLOOM_DB_CHARSET", "utf8mb4"),
    )


class HybridRow(dict):
    """A result row addressable by column name, position, or slice.

    Iteration yields **values**, matching ``sqlite3.Row`` and the DB-API rather
    than ``dict``. Inheriting dict's key iteration would make the most ordinary
    line in database code::

        for schema, table in cursor.fetchall():

    bind the column *names* to those variables — no exception, just wrong data
    that looks right until something downstream tries to do arithmetic on it.

    Key-based membership is preserved, so ``"col" in row`` still asks whether
    the column exists. Use ``row.values()`` or ``in tuple(row)`` to test values.
    """

    def __init__(self, columns: Sequence[str], values: Sequence[Any]):
        super().__init__(zip(columns, values))
        self._values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        return super().__getitem__(key)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __contains__(self, key: Any) -> bool:
        # Deliberately still key-based: `"column" in row` is the useful question.
        return dict.__contains__(self, key)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self):  # noqa: D102 - dict protocol, kept for ``dict(row)`` / ``**row``
        return dict.keys(self)


def _translate_placeholders(sql: str) -> str:
    """Translate sqlite-style '?' placeholders to PyMySQL '%s'."""
    parts = [part.replace("%", "%%") for part in sql.split("?")]
    return "%s".join(parts)


class _EmptyCursor:
    rowcount = 0
    lastrowid = None

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list:
        return []


class MySQLCursorAdapter:
    def __init__(self, cursor: Any):
        self._cursor = cursor
        self._columns: list[str] = []

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> Any:
        return self._cursor.lastrowid

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> Any:
        self._cursor.execute(_translate_placeholders(sql), params)
        self._columns = [col[0] for col in self._cursor.description or []]
        return self

    def executemany(self, sql: str, params_list: Iterable[Sequence[Any]]) -> Any:
        self._cursor.executemany(_translate_placeholders(sql), params_list)
        self._columns = [col[0] for col in self._cursor.description or []]
        return self

    def fetchone(self) -> Optional[HybridRow]:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return HybridRow(self._columns, row)

    def fetchall(self) -> list[HybridRow]:
        return [HybridRow(self._columns, row) for row in self._cursor.fetchall()]


class MySQLConnectionAdapter:
    def __init__(self, raw_connection: Any):
        self._conn = raw_connection
        self.row_factory = None

    def cursor(self) -> MySQLCursorAdapter:
        return MySQLCursorAdapter(self._conn.cursor())

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> Any:
        lowered = sql.strip().lower()
        if lowered.startswith("pragma "):
            return _EmptyCursor()
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def executemany(self, sql: str, params_list: Iterable[Sequence[Any]]) -> Any:
        cursor = self.cursor()
        cursor.executemany(sql, params_list)
        return cursor

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MySQLConnectionAdapter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def connect(
    timeout: float = 30.0,
    check_same_thread: bool = False,
    settings: Optional[DatabaseSettings] = None,
) -> MySQLConnectionAdapter:
    """Open a MySQL connection using AgentLoom runtime configuration."""
    del check_same_thread  # kept for call-site compatibility
    settings = settings or get_database_settings()
    if settings.driver != "mysql":
        raise RuntimeError(f"Unsupported database driver: {settings.driver}")

    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError(
            "PyMySQL is required. Install agentloom-runtime and configure DATABASE_URL "
            "or AGENTLOOM_DB_* variables."
        ) from exc
    if not all([settings.host, settings.database, settings.user]):
        raise RuntimeError(
            "MySQL configuration requires AGENTLOOM_DB_HOST, AGENTLOOM_DB_NAME, "
            "and AGENTLOOM_DB_USER, or a DATABASE_URL."
        )
    prod_name = os.environ.get("AGENTLOOM_DB_PROD_NAME", "").strip()
    if os.environ.get("AGENTLOOM_PYTEST") == "1" and prod_name and settings.database == prod_name:
        raise RuntimeError(
            f"pytest must not use production database '{prod_name}'. "
            "Set AGENTLOOM_TEST_DB_NAME to a dedicated test database."
        )
    raw = pymysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password or "",
        database=settings.database,
        charset=settings.charset,
        autocommit=False,
        connect_timeout=int(timeout),
    )
    return MySQLConnectionAdapter(raw)


def is_mysql() -> bool:
    return True


def is_sqlite() -> bool:
    return False
