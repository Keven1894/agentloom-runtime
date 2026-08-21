"""Tests for agentloom_runtime.db."""

from __future__ import annotations

import os

import pytest

from agentloom_runtime.db import DatabaseSettings, HybridRow, get_database_settings


def test_get_database_settings_from_agentloom_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGENTLOOM_DATABASE_URL", raising=False)
    monkeypatch.setenv("AGENTLOOM_DB_DRIVER", "mysql")
    monkeypatch.setenv("AGENTLOOM_DB_HOST", "db.example.com")
    monkeypatch.setenv("AGENTLOOM_DB_NAME", "app_db")
    monkeypatch.setenv("AGENTLOOM_DB_USER", "runtime")
    monkeypatch.setenv("AGENTLOOM_DB_PORT", "3307")

    settings = get_database_settings()
    assert settings == DatabaseSettings(
        driver="mysql",
        host="db.example.com",
        port=3307,
        database="app_db",
        user="runtime",
        password=None,
        charset="utf8mb4",
    )


def test_get_database_settings_from_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://db.example.com:3306/app_db")
    settings = get_database_settings()
    assert settings.host == "db.example.com"
    assert settings.database == "app_db"
    assert settings.port == 3306


# --------------------------------------------------------------------------
# HybridRow — addressable three ways, and iterable the way DB-API code expects
# --------------------------------------------------------------------------


def _row() -> HybridRow:
    return HybridRow(["schema", "table", "rows"], ["agentloom_prod", "agent_sessions", 2])


def test_row_is_addressable_by_name_and_position():
    row = _row()
    assert row["schema"] == "agentloom_prod"
    assert row[0] == "agentloom_prod"
    assert row[-1] == 2
    assert row[0:2] == ("agentloom_prod", "agent_sessions")


def test_unpacking_a_row_yields_values_not_column_names():
    """The bug this guards against produces wrong data, not an error.

    ``dict`` iterates keys, so unpacking a dict-derived row bound column names
    to the variables and everything downstream looked plausible.
    """
    schema, table, rows = _row()
    assert (schema, table, rows) == ("agentloom_prod", "agent_sessions", 2)
    assert list(_row()) == ["agentloom_prod", "agent_sessions", 2]


def test_row_still_behaves_as_a_mapping():
    row = _row()
    assert "schema" in row, "membership stays key-based: 'is there such a column'"
    assert dict(row) == {
        "schema": "agentloom_prod",
        "table": "agent_sessions",
        "rows": 2,
    }
    assert {**row}["table"] == "agent_sessions"
    assert sorted(row.keys()) == ["rows", "schema", "table"]
    assert len(row) == 3


def test_connect_blocks_prod_db_in_pytest(monkeypatch):
    monkeypatch.setenv("AGENTLOOM_PYTEST", "1")
    monkeypatch.setenv("AGENTLOOM_DB_PROD_NAME", "app_prod")
    monkeypatch.setenv("AGENTLOOM_DB_HOST", "localhost")
    monkeypatch.setenv("AGENTLOOM_DB_NAME", "app_prod")
    monkeypatch.setenv("AGENTLOOM_DB_USER", "runtime")

    from agentloom_runtime.db import connect

    with pytest.raises(RuntimeError, match="pytest must not use production"):
        connect()
