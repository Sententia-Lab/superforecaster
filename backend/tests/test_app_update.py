"""What `app.update` writes, given what the cycle concluded.

`superforecaster.update` decides; this module persists. The cycle's own routing rules
are covered in `test_graph_update.py`, which no longer knows a database exists — so the
writes are asserted here, once, against a stubbed outcome.
"""

from __future__ import annotations

import pytest

from app import update as au
from superforecaster.models import UpdateOutcome


@pytest.fixture
def written(monkeypatch):
    """Capture the two writes and stub the cycle. Returns knobs and what was written."""
    state = {
        "outcome": UpdateOutcome(updated=True, new_probability=0.60, reasoning="why"),
        "record": object(),
        "marked": [],
        "updates": [],
    }

    async def fake_cycle(record, deps=None):
        return state["outcome"]

    monkeypatch.setattr(au, "run_update_cycle", fake_cycle)
    monkeypatch.setattr(
        au.db, "get_forecast", lambda fid: _a_record(fid) if fid == "fc_1" else None
    )
    monkeypatch.setattr(
        au.db,
        "mark_refreshed",
        lambda fid, flagged: state["marked"].append((fid, flagged)),
    )
    monkeypatch.setattr(
        au.db, "add_forecast_update", lambda **kw: state["updates"].append(kw)
    )
    return state


def _a_record(fid: str):
    from tests.test_update_cycle import a_record

    record = a_record()
    return record.model_copy(update={"id": fid})


async def test_a_material_update_is_written_with_the_agents_reasoning(written):
    outcome = await au.run_update_graph("fc_1")

    assert outcome.updated is True
    assert written["updates"] == [
        {"forecast_id": "fc_1", "probability": 0.60, "reasoning": "why"}
    ]


async def test_a_no_op_writes_nothing(written):
    written["outcome"] = UpdateOutcome(updated=False, reason="below threshold")
    await au.run_update_graph("fc_1")

    assert written["updates"] == []


async def test_the_refreshed_timestamp_is_always_marked(written):
    """Even a no-op run records that the forecast was looked at."""
    written["outcome"] = UpdateOutcome(updated=False, reason="below threshold")
    await au.run_update_graph("fc_1")

    assert written["marked"] == [("fc_1", False)]


async def test_a_flagged_forecast_is_marked_flagged_and_never_written(written):
    written["outcome"] = UpdateOutcome(flagged_resolved=True, updated=False)
    await au.run_update_graph("fc_1")

    assert written["marked"] == [("fc_1", True)]
    assert written["updates"] == []


async def test_a_missing_forecast_is_a_storage_answer_not_a_cycle_run(written):
    """The cycle never runs, so nothing is marked either."""
    outcome = await au.run_update_graph("nope")

    assert outcome.reason == "forecast not found"
    assert written["marked"] == []
