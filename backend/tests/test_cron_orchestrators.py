"""Tests for cron orchestrators (digest + refresh).

We mock the agents — these tests verify the orchestration logic, not LLM calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from superforecaster import cron, db
from superforecaster.models import (
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
            SubPrediction(question=f"Sub {i}?", probability=0.5, rationale="r", confidence="medium")
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
    q1 = db.submit_question(text="Q1", resolution_criteria="X.", proposed_resolution_date=_future(), ip_hash="ip1")
    q2 = db.submit_question(text="Q2", resolution_criteria="X.", proposed_resolution_date=_future(), ip_hash="ip2")
    q3 = db.submit_question(text="Q3", resolution_criteria="X.", proposed_resolution_date=_future(), ip_hash="ip3")

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
    q1 = db.submit_question(text="Q1", resolution_criteria="X.", proposed_resolution_date=_future(), ip_hash="ip1")
    db.approve_question(q1.id)
    db.cast_vote(q1.id, ip_hash="v1", vote=1)

    promoted = cron.run_monthly_digest(n=5)
    assert promoted == []  # already-approved q1 not re-promoted


def test_preview_digest_does_not_mutate():
    q1 = db.submit_question(text="Q1", resolution_criteria="X.", proposed_resolution_date=_future(), ip_hash="ip1")
    db.cast_vote(q1.id, ip_hash="v1", vote=1)

    preview = cron.preview_monthly_digest(n=5)
    assert {p.id for p in preview} == {q1.id}

    # After preview, status is unchanged
    fresh = db.get_question(q1.id)
    assert fresh is not None
    assert fresh.status == "pending"


# ---------- Daily refresh ----------


@pytest.mark.asyncio
async def test_run_daily_refresh_calls_resolution_then_refresh():
    """When resolution flags a forecast, refresh should skip it."""
    f1 = _make_forecast(probability=0.5)
    f2 = _make_forecast(probability=0.5)
    f1_id = db.save_forecast(f1, resolution_source="x")
    f2_id = db.save_forecast(f2, resolution_source="x")

    # Mock: f1 flagged for resolution, f2 not
    async def mock_check_resolution(fid):
        if fid == f1_id:
            db.mark_refreshed(fid, flagged=True)
            return ResolutionCheckResult(
                appears_resolved=True,
                suggested_outcome=1.0,
                confidence="high",
                resolution_evidence="news source",
                reasoning="Event resolved.",
            )
        else:
            db.mark_refreshed(fid, flagged=False)
            return ResolutionCheckResult(
                appears_resolved=False,
                confidence="medium",
                reasoning="No evidence yet.",
            )

    async def mock_refresh_forecast(fid):
        # If resolution flagged this, refresh should not be called
        record = db.get_forecast(fid)
        assert record is not None
        if record.flagged_for_resolution_review:
            return RefreshActionResponse(updated=False, reason="flagged")
        return RefreshActionResponse(updated=False, reason="no change")

    with patch.object(cron, "check_resolution", side_effect=mock_check_resolution), \
         patch.object(cron, "refresh_forecast", side_effect=mock_refresh_forecast):
        summary = await cron.run_daily_refresh()

    assert summary.total_checked == 2
    assert summary.total_flagged_for_review == 1
    # f1 was flagged → skipped in sweep 2; f2 ran but no update
    assert summary.total_updated == 0
    assert summary.total_skipped == 2  # 1 flagged + 1 no-change

    # Verify f1 is flagged in DB
    f1_record = db.get_forecast(f1_id)
    assert f1_record is not None
    assert f1_record.flagged_for_resolution_review is True


@pytest.mark.asyncio
async def test_run_daily_refresh_records_run_history():
    f = _make_forecast()
    db.save_forecast(f, resolution_source="x")

    async def mock_check(fid):
        db.mark_refreshed(fid, flagged=False)
        return ResolutionCheckResult(appears_resolved=False, confidence="low", reasoning="no")

    async def mock_refresh(fid):
        return RefreshActionResponse(updated=False, reason="no change")

    with patch.object(cron, "check_resolution", side_effect=mock_check), \
         patch.object(cron, "refresh_forecast", side_effect=mock_refresh):
        await cron.run_daily_refresh()

    last_run = db.last_refresh_run()
    assert last_run is not None
    assert "total_checked" in last_run["summary"]
    assert last_run["summary"]["total_checked"] == 1


@pytest.mark.asyncio
async def test_refresh_forecast_skips_resolved():
    """refresh_forecast should be a no-op for already-resolved forecasts."""
    from superforecaster.refresh import refresh_forecast as rf

    f = _make_forecast()
    fid = db.save_forecast(f, resolution_source="x")
    db.resolve_forecast(fid, outcome=1.0)

    # No mock needed — should return early before calling agent
    result = await rf(fid)
    assert result.updated is False
    assert "resolved" in (result.reason or "").lower()


@pytest.mark.asyncio
async def test_refresh_forecast_skips_flagged():
    """refresh_forecast should skip forecasts that resolution flagged."""
    from superforecaster.refresh import refresh_forecast as rf

    f = _make_forecast()
    fid = db.save_forecast(f, resolution_source="x")
    db.mark_refreshed(fid, flagged=True)

    result = await rf(fid)
    assert result.updated is False
    assert "flag" in (result.reason or "").lower()


@pytest.mark.asyncio
async def test_refresh_forecast_threshold_blocks_small_changes():
    """If the agent's suggested change is below MIN_PROBABILITY_DELTA, no update."""
    from superforecaster import refresh as refresh_module
    from superforecaster.refresh import refresh_forecast as rf

    f = _make_forecast(probability=0.50)
    fid = db.save_forecast(f, resolution_source="x")

    async def mock_run(record):
        # Suggest a 1-point change, below the 3-point threshold
        return ForecastRefreshResult(
            should_update=True,
            new_probability=0.51,
            new_confidence="medium",
            reasoning="Minor news.",
        )

    with patch.object(refresh_module, "run_refresh_agent", side_effect=mock_run):
        result = await rf(fid)

    assert result.updated is False
    assert "threshold" in (result.reason or "").lower()

    # No new update row should exist beyond the initial one
    record = db.get_forecast(fid)
    assert record is not None
    assert len(record.updates) == 1


@pytest.mark.asyncio
async def test_refresh_forecast_writes_update_when_above_threshold():
    from superforecaster import refresh as refresh_module
    from superforecaster.refresh import refresh_forecast as rf

    f = _make_forecast(probability=0.50)
    fid = db.save_forecast(f, resolution_source="x")

    async def mock_run(record):
        return ForecastRefreshResult(
            should_update=True,
            new_probability=0.65,
            new_confidence="high",
            reasoning="Major new evidence.",
            evidence_found=["source 1", "source 2"],
        )

    with patch.object(refresh_module, "run_refresh_agent", side_effect=mock_run):
        result = await rf(fid)

    assert result.updated is True
    assert result.update is not None
    assert result.update.probability == 0.65

    record = db.get_forecast(fid)
    assert record is not None
    assert len(record.updates) == 2
    assert record.updates[-1].probability == 0.65
    assert record.last_refreshed_at is not None
