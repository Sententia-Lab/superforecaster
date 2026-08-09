"""Tests for the schema migration step.

These earn their place because the bug they guard against is invisible until the most
expensive possible moment. ADR 29 dropped `forecast_updates.confidence` from the INSERT
and from the fresh-schema DDL, but `CREATE TABLE IF NOT EXISTS` does nothing to a table
that already exists — so every deployed database kept a `NOT NULL` column nobody supplied.
Runs completed all five stages, produced a real forecast, and then died on the final write.

Nothing here touches the developer's database: `conftest._isolated_db` points
`DATABASE_PATH` at a tmp file, and the pre-migration fixture is built by hand.
"""

from __future__ import annotations

import sqlite3

import pytest

from superforecaster import db

# The `forecast_updates` schema exactly as it shipped before ADR 29 — including the
# column that stopped being written. Hand-built rather than checked in as a .db file so
# the thing being migrated *from* is readable in the diff.
PRE_ADR29 = """
CREATE TABLE forecasts (
    id TEXT PRIMARY KEY, question TEXT NOT NULL, resolution_criteria TEXT NOT NULL,
    resolution_source TEXT NOT NULL, category TEXT NOT NULL,
    submission_gap_days INTEGER NOT NULL DEFAULT 7, submission_deadline TIMESTAMP NOT NULL,
    resolution_date TIMESTAMP NOT NULL, resolved_at TIMESTAMP, outcome REAL,
    is_ambiguous INTEGER NOT NULL DEFAULT 0, scored_probability REAL, brier_score REAL,
    last_refreshed_at TIMESTAMP, flagged_for_resolution_review INTEGER NOT NULL DEFAULT 0,
    initial_reasoning TEXT NOT NULL, decompositions_json TEXT NOT NULL,
    research_json TEXT NOT NULL, created_at TIMESTAMP NOT NULL
);
CREATE TABLE forecast_updates (
    id TEXT PRIMARY KEY,
    forecast_id TEXT NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
    probability REAL NOT NULL,
    confidence TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    is_late INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL
);
"""


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    """A database at the pre-ADR-29 schema, with one row already in it."""
    path = tmp_path / "old.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    conn = sqlite3.connect(path)
    conn.executescript(PRE_ADR29)
    conn.execute(
        "INSERT INTO forecasts VALUES ('f1','q','c','s','general',7,'2026-01-01',"
        "'2026-12-31',NULL,NULL,0,NULL,NULL,NULL,0,'r','[]','{}','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO forecast_updates VALUES ('u1','f1',0.4,'medium','why',0,'2026-01-01')"
    )
    conn.commit()
    conn.close()
    return path


def columns(path, table="forecast_updates") -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def version(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_an_old_database_starts_at_version_zero(old_db):
    assert version(old_db) == 0
    assert "confidence" in columns(old_db)


def test_init_db_drops_the_column_adr_29_deleted(old_db):
    db.init_db()
    assert "confidence" not in columns(old_db)
    assert version(old_db) == db.SCHEMA_VERSION


def test_migrating_keeps_the_rows(old_db):
    """The whole reason to migrate rather than tell the operator to delete the file."""
    db.init_db()
    conn = sqlite3.connect(old_db)
    row = conn.execute(
        "SELECT id, probability, reasoning FROM forecast_updates"
    ).fetchone()
    conn.close()
    assert row == ("u1", 0.4, "why")


def test_a_migrated_database_accepts_the_write_that_used_to_fail(old_db):
    """The actual regression: `add_forecast_update` no longer supplies `confidence`, so
    against the old schema this raised `IntegrityError: NOT NULL constraint failed` —
    after the graph had already produced a forecast."""
    db.init_db()
    db.add_forecast_update("f1", probability=0.55, reasoning="new evidence")

    conn = sqlite3.connect(old_db)
    n = conn.execute("SELECT count(*) FROM forecast_updates").fetchone()[0]
    conn.close()
    assert n == 2


def test_init_db_is_idempotent(old_db):
    db.init_db()
    db.init_db()
    assert version(old_db) == db.SCHEMA_VERSION
    assert "confidence" not in columns(old_db)


def test_a_fresh_database_reaches_the_current_version(tmp_path, monkeypatch):
    """A new file is built at the current schema by `CREATE TABLE`, so every migration
    step finds its work already done — and has to tolerate that rather than raise."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "fresh.db"))
    db.init_db()

    assert version(tmp_path / "fresh.db") == db.SCHEMA_VERSION
    assert "confidence" not in columns(tmp_path / "fresh.db")


def test_v3_adds_edited_at_to_an_upgraded_database(old_db):
    """The upgrade route: `run_steps` is built by the create block during this same
    `init_db`, so migration 3's ALTER finds the column already there and must not raise.
    """
    db.init_db()

    assert "edited_at" in columns(old_db, table="run_steps")
    assert version(old_db) == db.SCHEMA_VERSION


def test_v3_adds_edited_at_to_a_database_that_already_had_run_steps(
    tmp_path, monkeypatch
):
    """The other route: a version-2 database whose `run_steps` predates the column, so
    the ALTER runs for real. This is what a deployed database actually looks like."""
    path = tmp_path / "v2.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    conn = sqlite3.connect(path)
    conn.executescript(PRE_ADR29)
    conn.executescript(
        """
        CREATE TABLE gated_runs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'backlog',
            created_at TIMESTAMP NOT NULL
        );
        CREATE TABLE run_steps (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
            sub_claim_id TEXT NOT NULL DEFAULT '', lens_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', payload_json TEXT, error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            started_at TIMESTAMP, finished_at TIMESTAMP
        );
        """
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    assert "edited_at" not in columns(path, table="run_steps")
    db.init_db()

    assert "edited_at" in columns(path, table="run_steps")
    assert version(path) == db.SCHEMA_VERSION


V3_PAYLOAD = {
    "lens": {
        "name": "post-tightening years",
        "population": "every year since 1970",
        "why_it_fits": "same inflection",
        "weight": 1.0,
        "weight_rationale": "only lens",
        "evidence": [{"kind": "counted", "hits": 8, "n": 11, "note": "enumerated"}],
        "sub_claim_ids": ["sc2"],
    },
    "disagreement": "search was unavailable",
}
"""One `base_rates` payload exactly as version 3 wrote it — old keys, old ids."""


@pytest.fixture
def v3_db(tmp_path, monkeypatch):
    """A version-3 database holding one run whose payloads use the old vocabulary."""
    import json

    path = tmp_path / "v3.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    conn = sqlite3.connect(path)
    conn.executescript(PRE_ADR29)
    conn.executescript(
        """
        CREATE TABLE gated_runs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'backlog',
            created_at TIMESTAMP NOT NULL
        );
        CREATE TABLE run_steps (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
            sub_claim_id TEXT NOT NULL DEFAULT '', lens_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', payload_json TEXT, error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            started_at TIMESTAMP, finished_at TIMESTAMP, edited_at TIMESTAMP
        );
        """
    )
    conn.execute("INSERT INTO gated_runs VALUES ('r1','active','2026-01-01')")
    conn.execute(
        "INSERT INTO run_steps (id, run_id, stage, sub_claim_id, lens_name, status, "
        "payload_json) VALUES ('s1','r1','base_rates','sc2','post-tightening years',"
        "'complete', ?)",
        (json.dumps(V3_PAYLOAD),),
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    return path


def test_v4_renames_the_column_and_the_ids(v3_db):
    db.init_db()

    assert "sub_claim_id" not in columns(v3_db, table="run_steps")
    assert "sub_question_id" in columns(v3_db, table="run_steps")

    conn = sqlite3.connect(v3_db)
    row = conn.execute("SELECT sub_question_id FROM run_steps").fetchone()
    conn.close()
    assert row[0] == "sq2"


def test_v4_rewrites_the_stored_payload(v3_db):
    """The payload is what an ORM-free system actually stores, so it is what a rename has
    to reach. A step whose column was renamed but whose blob was not stops parsing."""
    import json

    from superforecaster.models import BaseRateStepPayload

    db.init_db()

    conn = sqlite3.connect(v3_db)
    raw = conn.execute("SELECT payload_json FROM run_steps").fetchone()[0]
    conn.close()

    assert "sub_claim" not in raw
    assert json.loads(raw)["lens"]["sub_question_ids"] == ["sq2"]
    BaseRateStepPayload.model_validate_json(raw)


def test_v4_leaves_prose_that_merely_starts_with_sc_alone():
    """The regression a textual `replace(payload_json, '"sc', '"sq')` would cause, and the
    whole reason migration 4 has a Python step (ADR 57)."""
    payload = {
        "bias_checks": [{"bias": "scope_insensitivity", "assessment": "considered"}],
        "research": {"causal_forces": ["scarcity of chips", "schedule slippage"]},
        "note": "sc2 is mentioned in prose here",
        "sub_claim_ids": ["sc2"],
    }

    out = db._rename_sub_claims(payload)

    assert out["bias_checks"][0]["bias"] == "scope_insensitivity"
    assert out["research"]["causal_forces"] == [
        "scarcity of chips",
        "schedule slippage",
    ]
    assert out["note"] == "sc2 is mentioned in prose here"
    assert out["sub_question_ids"] == ["sq2"]


def test_v4_is_a_no_op_on_a_fresh_database(tmp_path, monkeypatch):
    """`RENAME COLUMN` against a database born with `sub_question_id` raises "no such
    column", which `_MIGRATION_NO_OPS` is there to swallow."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "fresh4.db"))
    db.init_db()

    assert version(tmp_path / "fresh4.db") == db.SCHEMA_VERSION
    assert "sub_question_id" in columns(tmp_path / "fresh4.db", table="run_steps")


def test_every_version_up_to_current_has_a_step():
    """A bumped `SCHEMA_VERSION` with no matching entry migrates nothing and silently
    marks the database as done."""
    assert set(db.MIGRATIONS) == set(range(1, db.SCHEMA_VERSION + 1))
