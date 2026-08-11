"""Shared pytest fixtures.

Each test gets a fresh, isolated SQLite database via DATABASE_PATH env override,
and a dummy model string so agents can be constructed without any API key.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from superforecaster import db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point each test at its own temporary SQLite file."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    db.init_db()
    yield db_file


@pytest.fixture(autouse=True, scope="session")
def _quiet_unconfigured_logfire():
    """Core emits spans but never configures Logfire — that is the library contract.

    Nothing configures it under pytest either, so every span would print
    `LogfireNotConfiguredWarning`. Suppressed here rather than in `pyproject.toml`,
    because in production the warning is a real signal that tracing is off.
    """
    os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")


@pytest.fixture(autouse=True)
def _stub_agent_model(monkeypatch):
    """Let agents build without a real API key.

    `resolve_agent_model()` raises when no key is set, so any test that constructs
    an Agent fails on a machine with no `backend/.env`. Tests never reach a provider
    — they use `TestModel`/`FunctionModel` via `agent.override` — but the model
    string still has to name a real provider for the Agent to build, so this is a
    valid string plus a fake key rather than a placeholder.

    `test_config.py` sets or deletes both vars itself where it matters.
    """
    monkeypatch.setenv("AGENT_MODEL", "anthropic:claude-sonnet-4-6")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")


@pytest.fixture(autouse=True)
def _unset_admin_key(monkeypatch):
    """Run every test in local mode unless it asks otherwise.

    `backend/.env` is loaded at import, so a developer who set `ADMIN_API_KEY` — which is
    every developer who has deployed this — had `require_admin` reject the test client on
    every admin route. Fourteen tests failed on their machine and passed in CI, which is
    the worst way round. A test that wants authentication sets the key itself.
    """
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "superforecaster" / "fixtures"
