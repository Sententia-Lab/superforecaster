"""Tests for the daily refresh orchestrator.

We mock the agents — these tests verify the orchestration logic, not LLM calls.
`run_daily_refresh` is a single loop over `run_update_graph`, so these tests cover
the sweep bookkeeping only. The rules that used to live here — resolution blocking
the probability update, the delta threshold, the write gate — are graph routing now
and are tested in `test_graph_update.py`, where they belong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from superforecaster import cron, db
from superforecaster.models import (
    Forecast,
    HistoricalAnalog,
    ResearchSummary,
    SubPrediction,
    UpdateOutcome,
)


def _make_forecast(probability: float = 0.5) -> Forecast:
    return Forecast(
        question="Will X happen?",
        resolution_criteria="X is observably true.",
        resolution_date=datetime.now(timezone.utc) + timedelta(days=60),
        category="test",
        probability=probability,
        decompositions=[
            SubPrediction(question=f"Sub {i}?", probability=0.5, rationale="r")
            for i in range(3)
        ],
        research=ResearchSummary(
            historical_analogs=[
                HistoricalAnalog(description="A", outcome=1.0, relevance="r"),
                HistoricalAnalog(description="B", outcome=0.0, relevance="r"),
                HistoricalAnalog(description="C", outcome=1.0, relevance="r"),
            ],
            empirical_base_rate=2 / 3,
            base_rate_note="ok",
        ),
        reasoning="Test.",
    )


@pytest.mark.asyncio
async def test_run_daily_refresh_counts_each_outcome_kind():
    """Flagged, updated, and no-change forecasts land in the right counters."""
    ids = [
        db.save_forecast(_make_forecast(probability=0.5), resolution_source="x")
        for _ in range(3)
    ]
    flagged_id, updated_id, quiet_id = ids

    async def fake_update_graph(fid, *, verbose=False):
        if fid == flagged_id:
            return UpdateOutcome(
                flagged_resolved=True, updated=False, reason="resolved"
            )
        if fid == updated_id:
            return UpdateOutcome(updated=True, new_probability=0.62, reason="moved")
        return UpdateOutcome(updated=False, reason="no change")

    with patch.object(cron, "run_update_graph", side_effect=fake_update_graph):
        summary = await cron.run_daily_refresh()

    assert summary.total_checked == 3
    assert summary.total_flagged_for_review == 1
    assert summary.total_updated == 1
    assert summary.total_skipped == 2  # the flagged one plus the no-change one


@pytest.mark.asyncio
async def test_run_daily_refresh_survives_one_bad_forecast():
    """One forecast raising must not abort the sweep for the others."""
    ids = [db.save_forecast(_make_forecast(), resolution_source="x") for _ in range(3)]
    exploding = ids[1]

    async def fake_update_graph(fid, *, verbose=False):
        if fid == exploding:
            raise RuntimeError("provider exploded")
        return UpdateOutcome(updated=True, new_probability=0.6, reason="moved")

    with patch.object(cron, "run_update_graph", side_effect=fake_update_graph):
        summary = await cron.run_daily_refresh()

    assert summary.total_checked == 3
    assert summary.total_updated == 2
    assert len(summary.errors) == 1
    assert "provider exploded" in summary.errors[0]
