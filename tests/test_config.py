"""Environment discovery.

The rule that matters: a ``.env`` file fills gaps, it never overrides the real
process environment. Getting that backwards would let a stray file on a laptop
silently redirect a production job.
"""

from __future__ import annotations

import os

import pytest

from agentloom_runtime import config


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Snapshot the whole environment, not just the keys a test names.

    ``load_env`` writes to ``os.environ`` directly, so monkeypatch cannot undo
    a variable it never saw being set. Without a full restore, one test's
    ``.env`` fixture leaks into every test that runs after it.
    """
    monkeypatch.setattr(config, "_loaded", set())
    original = dict(os.environ)
    monkeypatch.delenv("AGENTLOOM_ENV_FILE", raising=False)
    yield
    os.environ.clear()
    os.environ.update(original)


def _write_env(path, body: str):
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config.load_env() is None


def test_values_are_read_from_the_working_directory(tmp_path, monkeypatch):
    _write_env(tmp_path / ".env", "AGENTLOOM_DB_HOST=db.example.org\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTLOOM_DB_HOST", raising=False)

    config.load_env()
    assert os.environ["AGENTLOOM_DB_HOST"] == "db.example.org"


def test_a_real_environment_variable_always_wins(tmp_path, monkeypatch):
    _write_env(tmp_path / ".env", "AGENTLOOM_DB_NAME=from_file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTLOOM_DB_NAME", "from_environment")

    config.load_env()
    assert os.environ["AGENTLOOM_DB_NAME"] == "from_environment"


def test_a_nested_working_directory_still_finds_the_repository_root(tmp_path, monkeypatch):
    _write_env(tmp_path / ".env", "AGENTLOOM_DB_USER=root_level\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("AGENTLOOM_DB_USER", raising=False)

    config.load_env()
    assert os.environ["AGENTLOOM_DB_USER"] == "root_level"


def test_explicit_env_file_beats_directory_discovery(tmp_path, monkeypatch):
    _write_env(tmp_path / ".env", "AGENTLOOM_DB_PORT=1111\n")
    pinned = _write_env(tmp_path / "pinned.env", "AGENTLOOM_DB_PORT=2222\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTLOOM_ENV_FILE", str(pinned))
    monkeypatch.delenv("AGENTLOOM_DB_PORT", raising=False)

    config.load_env()
    assert os.environ["AGENTLOOM_DB_PORT"] == "2222"


def test_quotes_comments_and_blank_lines_are_handled(tmp_path, monkeypatch):
    _write_env(
        tmp_path / ".env",
        '\n# a comment\n\nAGENTLOOM_DB_PASSWORD="qu0ted!value"\nEMBEDDING_MODEL=\'single\'\n',
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTLOOM_DB_PASSWORD", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)

    config.load_env()
    assert os.environ["AGENTLOOM_DB_PASSWORD"] == "qu0ted!value"
    assert os.environ["EMBEDDING_MODEL"] == "single"


def test_a_value_containing_equals_is_not_truncated(tmp_path, monkeypatch):
    """Connection strings and base64 secrets routinely contain '='."""
    _write_env(tmp_path / ".env", "DATABASE_URL=mysql://u:p@h:3306/db?charset=utf8mb4\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    config.load_env()
    assert os.environ["DATABASE_URL"] == "mysql://u:p@h:3306/db?charset=utf8mb4"


def test_the_database_and_embedding_layers_share_one_loader(tmp_path, monkeypatch):
    """The bug this module exists to prevent.

    When each layer had its own rules, the adapter could find configuration the
    embedding provider could not — so an archive was fully embedded while every
    query silently degraded to lexical-only.
    """
    _write_env(
        tmp_path / ".env",
        "AGENTLOOM_DB_HOST=shared.example.org\nEMBEDDING_MODEL=shared-model\n",
    )
    monkeypatch.chdir(tmp_path)
    for name in ("AGENTLOOM_DB_HOST", "EMBEDDING_MODEL", "OPENAI_EMBEDDING_MODEL"):
        monkeypatch.delenv(name, raising=False)

    from agentloom_runtime.db.adapter import get_database_settings
    from agentloom_runtime.memory.embedding_provider import get_embedding_model

    assert get_database_settings().host == "shared.example.org"
    assert get_embedding_model() == "shared-model"
