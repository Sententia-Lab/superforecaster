"""The gated-run persistence layer: round-trips, the claim CAS, the restart sweep."""

from __future__ import annotations

import sqlite3

import pytest

from app import db
from superforecaster.models import (
    BaseRateStepPayload,
    Decomposition,
    InsideStepPayload,
    SubQuestionLenses,
    SynthesisStepPayload,
)

from .gated_factories import (
    base_rate_payload,
    chosen_lenses,
    decomposition,
    future,
    inside_payload,
    synthesis_payload,
)


def _run(**kwargs) -> dict:
    defaults = dict(
        question="Will it happen?",
        resolution_criteria="It observably happens.",
        resolution_source="the registry",
        resolution_date=future(),
    )
    return db.create_gated_run(**{**defaults, **kwargs})


# ---------- payload round-trips ----------


@pytest.mark.parametrize(
    "model_cls, instance",
    [
        (Decomposition, decomposition()),
        (SubQuestionLenses, chosen_lenses("a", "b")),
        (BaseRateStepPayload, base_rate_payload()),
        (InsideStepPayload, inside_payload()),
        (SynthesisStepPayload, synthesis_payload()),
    ],
)
def test_every_payload_shape_round_trips(model_cls, instance):
    """What a step persists must validate back into the model that produced it."""
    run = _run()
    db.insert_steps(run["id"], [("decompose", "", "")])
    step = db.list_steps(run["id"])[0]
    db.claim_step(step["id"])
    db.finish_step(step["id"], instance.model_dump_json())

    stored = db.get_step(step["id"])
    assert stored is not None
    restored = model_cls.model_validate_json(stored["payload_json"])
    assert restored == instance


# ---------- run lifecycle ----------


def test_create_lands_in_backlog_with_partial_fields():
    run = db.create_gated_run(question="Just an idea")
    assert run["status"] == "backlog"
    assert run["resolution_date"] is None


def test_edit_only_in_backlog():
    run = _run()
    db.update_gated_run_fields(run["id"], question="Sharper?")
    db.start_gated_run(run["id"])
    with pytest.raises(db.StateError):
        db.update_gated_run_fields(run["id"], question="Too late")


def test_start_is_a_cas():
    run = _run()
    db.start_gated_run(run["id"])
    with pytest.raises(db.StateError):
        db.start_gated_run(run["id"])


def test_delete_cascades_steps():
    run = _run()
    db.insert_steps(run["id"], [("decompose", "", "")])
    db.delete_gated_run(run["id"])
    assert db.get_gated_run(run["id"]) is None
    assert db.list_steps(run["id"]) == []


def test_list_includes_stage_counts_and_error_chip():
    run = _run()
    db.insert_steps(run["id"], [("decompose", "", "")])
    step = db.list_steps(run["id"])[0]
    db.claim_step(step["id"])
    db.fail_step(step["id"], "boom")

    listed = {r["id"]: r for r in db.list_gated_runs()}[run["id"]]
    assert listed["error"] == "boom"
    assert listed["stage_counts"]["decompose"]["error"] == 1


# ---------- the claim CAS ----------


def test_claim_rejects_a_running_step():
    run = _run()
    db.insert_steps(run["id"], [("decompose", "", "")])
    step = db.list_steps(run["id"])[0]
    assert db.claim_step(step["id"]) is not None
    assert db.claim_step(step["id"]) is None


def test_retry_reclaims_an_errored_step_and_clears_the_chip():
    run = _run()
    db.insert_steps(run["id"], [("decompose", "", "")])
    step = db.list_steps(run["id"])[0]
    db.claim_step(step["id"])
    db.fail_step(step["id"], "boom")

    reclaimed = db.claim_step(step["id"])
    assert reclaimed is not None
    assert reclaimed["status"] == "running"
    assert reclaimed["attempts"] == 2
    assert db.get_gated_run(run["id"])["error"] is None


def test_insert_steps_is_idempotent():
    run = _run()
    db.insert_steps(run["id"], [("lenses", "sq1", "")])
    db.insert_steps(run["id"], [("lenses", "sq1", "")])
    assert len(db.list_steps(run["id"])) == 1


# ---------- restart sweep ----------


def test_interrupted_steps_are_marked_on_init():
    run = _run()
    db.insert_steps(run["id"], [("decompose", "", "")])
    step = db.list_steps(run["id"])[0]
    db.claim_step(step["id"])

    assert db.mark_interrupted_steps() == 1
    fresh = db.get_step(step["id"])
    assert fresh["status"] == "error"
    assert "interrupted" in fresh["error"]
    assert "interrupted" in db.get_gated_run(run["id"])["error"]


# ---------- migration v2 ----------


def test_migration_v2_drops_community_tables(tmp_path, monkeypatch):
    """A v1 database with the old tables loses them; a fresh one never has them."""
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE forecasts (id TEXT PRIMARY KEY);
        CREATE TABLE questions (id TEXT PRIMARY KEY);
        CREATE TABLE votes (id TEXT PRIMARY KEY);
        CREATE TABLE refresh_runs (id TEXT PRIMARY KEY);
        CREATE TABLE runs (id TEXT PRIMARY KEY);
        PRAGMA user_version = 1;
        """)
    conn.close()

    monkeypatch.setenv("DATABASE_PATH", str(old))
    db.init_db()

    conn = sqlite3.connect(old)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert version == db.SCHEMA_VERSION
    assert "questions" not in tables
    assert "votes" not in tables
    assert "refresh_runs" not in tables
    assert "runs" not in tables
    assert {"gated_runs", "run_steps"} <= tables


def test_an_adjustment_without_a_title_still_validates():
    """ADR 58 gave `Adjustment` a title with `default=""`, so every payload written before
    the field existed keeps parsing. The frontend falls back to `evidence` for those."""
    from superforecaster.models import Adjustment

    a = Adjustment(
        evidence="a recent shift",
        direction="up",
        magnitude=0.05,
        flip_test="the opposite would move it down",
    )

    assert a.title == ""
    assert Adjustment.model_validate_json(a.model_dump_json()) == a
