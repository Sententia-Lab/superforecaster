"""Shared pytest fixtures.

Each test gets a fresh, isolated SQLite database via DATABASE_PATH env override.
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


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "superforecaster" / "fixtures"
