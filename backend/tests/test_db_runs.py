"""Tests for the `runs` table.

Only identity and terminal state are stored — no events. What matters here is that a
run started but never finished is recoverable as `lost` rather than left claiming to
be running forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from superforecaster import db
from tests.test_graph_forecast import a_forecast


def a_saved_forecast_id() -> str:
    """`runs.forecast_id` has a real foreign key, so a run can only point at a
    forecast that exists — which in production it always does, because `execute`
    saves the forecast before it finishes the run."""
    return db.save_forecast(a_forecast(), resolution_source="test")


def make(run_id: str = "run_abc", **kw) -> str:
    db.create_run(
        run_id=run_id,
        question=kw.get("question", "Will X happen?"),
        resolution_criteria=kw.get("resolution_criteria", "X is observable."),
        resolution_source=kw.get("resolution_source", ""),
        resolution_date=kw.get(
            "resolution_date", datetime.now(timezone.utc) + timedelta(days=30)
        ),
        category=kw.get("category", "test"),
    )
    return run_id


def test_a_new_run_is_queued():
    row = db.get_run(make())
    assert row["status"] == "queued"
    assert row["ended_at"] is None
    assert row["forecast_id"] is None


def test_finish_run_writes_the_terminal_state():
    fid = a_saved_forecast_id()
    db.finish_run(make(), status="done", forecast_id=fid)
    row = db.get_run("run_abc")

    assert row["status"] == "done"
    assert row["forecast_id"] == fid
    assert row["ended_at"] is not None


def test_finish_run_is_idempotent():
    """It is called from a `finally`, which can be reached more than once on a
    cancellation that races the happy path."""
    fid = a_saved_forecast_id()
    run_id = make()
    db.finish_run(run_id, status="done", forecast_id=fid)
    db.finish_run(run_id, status="done", forecast_id=fid)

    assert db.get_run(run_id)["status"] == "done"


def test_a_run_cannot_point_at_a_forecast_that_does_not_exist():
    """The foreign key is deliberate: a run claiming a forecast id nothing can resolve
    would show the UI a dead link."""
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        db.finish_run(make(), status="done", forecast_id="not-a-real-forecast")


def test_an_error_run_keeps_its_message():
    db.finish_run(make(), status="error", error="RuntimeError: provider exploded")
    assert "provider exploded" in db.get_run("run_abc")["error"]


def test_get_unknown_run_is_none():
    assert db.get_run("run_nope") is None


def test_list_runs_is_newest_first_and_filterable():
    make("run_1")
    make("run_2")
    db.finish_run("run_1", status="done")

    assert [r["id"] for r in db.list_runs()] == ["run_2", "run_1"]
    assert [r["id"] for r in db.list_runs(status="done")] == ["run_1"]


def test_orphaned_runs_are_marked_lost():
    """A run lives in memory. Anything still live after a restart is gone, and the row
    has to say so or the UI waits forever on a stream nobody will ever write to."""
    make("run_live")
    make("run_done")
    db.finish_run("run_done", status="done")

    assert db.mark_orphaned_runs_lost() == 1
    assert db.get_run("run_live")["status"] == "lost"
    assert db.get_run("run_live")["ended_at"] is not None
    assert db.get_run("run_done")["status"] == "done"


def test_init_db_reaps_orphans_on_boot():
    make("run_live")
    db.init_db()
    assert db.get_run("run_live")["status"] == "lost"
