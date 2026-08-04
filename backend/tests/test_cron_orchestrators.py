"""Tests for cron orchestrators (digest + refresh).

We mock the agents — these tests verify the orchestration logic, not LLM calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from superforecaster import cron, db
from superforecaster.models import (
    UpdateOutcome,
    Forecast,
    ForecastRefreshResult,
    HistoricalAnalog,
    RefreshActionResponse,
    ResearchSummary,
    ResolutionCheckResult,
    SubPrediction,
)


def _make_forecast(probability: float = 0.5) -> Forecast:
    return Forecast(
        question="Will X happen?",
        resolution_criteria="X is observably true.",
        resolution_date=datetime.now(timezone.utc) + timedelta(days=60),
        category="test",
        probability=probability,
        confidence="medium",
        decompositions=[
            SubPrediction(
                question=f"Sub {i}?",
                probability=0.5,
                rationale="r",
                confidence="medium",
            )
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


# ---------- Monthly digest ----------


def _future(days: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def test_digest_promotes_top_pending_questions_to_approved():
    q1 = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future(),
        ip_hash="ip1",
    )
    q2 = db.submit_question(
        text="Q2",
        resolution_criteria="X.",
        proposed_resolution_date=_future(),
        ip_hash="ip2",
    )
    q3 = db.submit_question(
        text="Q3",
        resolution_criteria="X.",
        proposed_resolution_date=_future(),
        ip_hash="ip3",
    )

    db.cast_vote(q1.id, ip_hash="v1", vote=1)
    db.cast_vote(q1.id, ip_hash="v2", vote=1)
    db.cast_vote(q2.id, ip_hash="v1", vote=1)
    # q3 has 0 votes

    promoted = cron.run_monthly_digest(n=2)

    assert len(promoted) == 2
    assert {p.id for p in promoted} == {q1.id, q2.id}
    assert all(p.status == "approved" for p in promoted)

    # Untouched
    fresh_q3 = db.get_question(q3.id)
    assert fresh_q3 is not None
    assert fresh_q3.status == "pending"


def test_digest_skips_already_approved_questions():
    q1 = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future(),
        ip_hash="ip1",
    )
    db.approve_question(q1.id)
    db.cast_vote(q1.id, ip_hash="v1", vote=1)

    promoted = cron.run_monthly_digest(n=5)
    assert promoted == []  # already-approved q1 not re-promoted


def test_preview_digest_does_not_mutate():
    q1 = db.submit_question(
        text="Q1",
        resolution_criteria="X.",
        proposed_resolution_date=_future(),
        ip_hash="ip1",
    )
    db.cast_vote(q1.id, ip_hash="v1", vote=1)

    preview = cron.preview_monthly_digest(n=5)
    assert {p.id for p in preview} == {q1.id}

    # After preview, status is unchanged
    fresh = db.get_question(q1.id)
    assert fresh is not None
    assert fresh.status == "pending"


# ---------- Daily refresh ----------
#
# `run_daily_refresh` is now a single loop over `run_update_graph`, so these tests
# cover the sweep bookkeeping only. The rules that used to live here — resolution
# blocking the probability update, the delta threshold, the write gate — are graph
# routing now and are tested in `test_graph_update.py`, where they belong.


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


@pytest.mark.asyncio
async def test_run_daily_refresh_records_run_history():
    db.save_forecast(_make_forecast(), resolution_source="x")

    async def fake_update_graph(fid, *, verbose=False):
        return UpdateOutcome(updated=False, reason="no change")

    with patch.object(cron, "run_update_graph", side_effect=fake_update_graph):
        await cron.run_daily_refresh()

    last_run = db.last_refresh_run()
    assert last_run is not None
    assert last_run["summary"]["total_checked"] == 1
