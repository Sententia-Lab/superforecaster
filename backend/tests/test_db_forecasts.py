"""Tests for forecast persistence and time-weighted scoring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from superforecaster import db
from superforecaster.models import (
    Forecast,
    HistoricalAnalog,
    ResearchSummary,
    SubPrediction,
)


def _make_forecast(
    question: str = "Will X happen?",
    resolution_date: datetime | None = None,
    probability: float = 0.5,
) -> Forecast:
    if resolution_date is None:
        resolution_date = datetime.now(timezone.utc) + timedelta(days=90)
    return Forecast(
        question=question,
        resolution_criteria="X is observably true by resolution_date.",
        resolution_date=resolution_date,
        category="test",
        probability=probability,
        decompositions=[
            SubPrediction(question="Sub 1?", probability=0.5, rationale="r1"),
            SubPrediction(question="Sub 2?", probability=0.5, rationale="r2"),
            SubPrediction(question="Sub 3?", probability=0.5, rationale="r3"),
        ],
        research=ResearchSummary(
            historical_analogs=[
                HistoricalAnalog(description="A", outcome=1.0, relevance="r"),
                HistoricalAnalog(description="B", outcome=0.0, relevance="r"),
                HistoricalAnalog(description="C", outcome=1.0, relevance="r"),
            ],
            empirical_base_rate=2 / 3,
            base_rate_note="ok",
            causal_forces=["force1"],
            evidence={"supporting": ["a"], "contradicting": ["b"]},
            uncertainties=["u1"],
        ),
        reasoning="Test reasoning.",
    )


def test_save_and_get_forecast_roundtrips():
    f = _make_forecast(probability=0.42)
    fid = db.save_forecast(f, resolution_source="test source")

    record = db.get_forecast(fid)
    assert record is not None
    assert record.question == f.question
    assert record.resolution_criteria == f.resolution_criteria
    assert record.category == "test"
    assert record.outcome is None
    assert record.is_ambiguous is False
    # The initial save creates one update row
    assert len(record.updates) == 1
    assert record.updates[0].probability == pytest.approx(0.42)
    assert record.updates[0].is_late is False


def test_submission_deadline_is_resolution_minus_gap():
    resolution = datetime(2027, 1, 1, tzinfo=timezone.utc)
    f = _make_forecast(resolution_date=resolution)
    fid = db.save_forecast(f, resolution_source="x", submission_gap_days=14)

    record = db.get_forecast(fid)
    assert record is not None
    assert record.submission_gap_days == 14
    assert record.submission_deadline == resolution - timedelta(days=14)


def test_add_update_appends_row():
    f = _make_forecast(probability=0.5)
    fid = db.save_forecast(f, resolution_source="x")

    update = db.add_forecast_update(fid, probability=0.6, reasoning="new evidence")
    assert update.probability == pytest.approx(0.6)
    assert update.is_late is False

    record = db.get_forecast(fid)
    assert record is not None
    assert len(record.updates) == 2
    assert record.updates[-1].probability == pytest.approx(0.6)


def test_add_update_after_resolution_date_raises():
    """Cannot update after resolution_date has passed."""
    f = _make_forecast(resolution_date=datetime.now(timezone.utc) - timedelta(days=1))
    # Need to save first; submission_deadline check is at submit time, not save
    fid = db.save_forecast(f, resolution_source="x", submission_gap_days=0)

    with pytest.raises(db.StateError, match="update deadline"):
        db.add_forecast_update(fid, probability=0.7, reasoning="r")


def test_late_flag_set_within_24h_of_resolution():
    resolution = datetime.now(timezone.utc) + timedelta(hours=12)
    f = _make_forecast(resolution_date=resolution)
    fid = db.save_forecast(f, resolution_source="x", submission_gap_days=0)

    update = db.add_forecast_update(fid, probability=0.6, reasoning="r")
    assert update.is_late is True


def test_time_weighted_average_matches_spec_example():
    """The spec example: day 0 → 30%, day 60 → 50%, resolve day 90 → 36.67%."""
    base = datetime.now(timezone.utc) - timedelta(days=90)
    resolution = base + timedelta(days=90)

    f = _make_forecast(resolution_date=resolution, probability=0.30)
    fid = db.save_forecast(f, resolution_source="x", submission_gap_days=0)

    # Manually rewrite the initial update's timestamp to "day 0"
    with db.connect() as conn:
        conn.execute(
            "UPDATE forecast_updates SET created_at = ? WHERE forecast_id = ?",
            (base, fid),
        )

    # Insert a second update at day 60
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO forecast_updates (id, forecast_id, probability, reasoning, is_late, created_at)
            VALUES ('update-2', ?, 0.50, 'change', 0, ?)
            """,
            (fid, base + timedelta(days=60)),
        )

    twa = db.compute_time_weighted_probability(fid)
    expected = (0.30 * 60 + 0.50 * 30) / 90
    assert twa == pytest.approx(expected, abs=1e-4)


def test_resolve_forecast_computes_brier():
    base = datetime.now(timezone.utc) - timedelta(days=90)
    resolution = base + timedelta(days=90)
    f = _make_forecast(resolution_date=resolution, probability=0.70)
    fid = db.save_forecast(f, resolution_source="x", submission_gap_days=0)

    with db.connect() as conn:
        conn.execute(
            "UPDATE forecast_updates SET created_at = ? WHERE forecast_id = ?",
            (base, fid),
        )

    db.resolve_forecast(fid, outcome=1.0)

    record = db.get_forecast(fid)
    assert record is not None
    assert record.outcome == 1.0
    assert record.scored_probability == pytest.approx(0.70, abs=1e-4)
    assert record.brier_score == pytest.approx((0.70 - 1.0) ** 2, abs=1e-4)
    assert record.resolved_at is not None


def test_resolve_with_none_marks_ambiguous():
    f = _make_forecast()
    fid = db.save_forecast(f, resolution_source="x")

    db.resolve_forecast(fid, outcome=None)

    record = db.get_forecast(fid)
    assert record is not None
    assert record.is_ambiguous is True
    assert record.outcome is None
    assert record.scored_probability is None
    assert record.brier_score is None


def test_cannot_resolve_twice():
    f = _make_forecast()
    fid = db.save_forecast(f, resolution_source="x")
    db.resolve_forecast(fid, outcome=1.0)

    with pytest.raises(db.StateError, match="already resolved"):
        db.resolve_forecast(fid, outcome=0.0)


def test_calibration_report_excludes_ambiguous_and_unresolved():
    base = datetime.now(timezone.utc) - timedelta(days=90)
    resolution = base + timedelta(days=90)

    # Resolved (will be included)
    f1 = _make_forecast(resolution_date=resolution, probability=0.7)
    f1_id = db.save_forecast(f1, resolution_source="x", submission_gap_days=0)
    with db.connect() as conn:
        conn.execute(
            "UPDATE forecast_updates SET created_at = ? WHERE forecast_id = ?",
            (base, f1_id),
        )
    db.resolve_forecast(f1_id, outcome=1.0)

    # Ambiguous (excluded)
    f2 = _make_forecast(resolution_date=resolution, probability=0.5)
    f2_id = db.save_forecast(f2, resolution_source="x", submission_gap_days=0)
    db.resolve_forecast(f2_id, outcome=None)

    # Unresolved (excluded)
    db.save_forecast(_make_forecast(probability=0.4), resolution_source="x")

    report = db.calibration_report()
    assert report.total_resolved == 1
    assert report.total_ambiguous_excluded == 1
    assert report.aggregate_brier_score == pytest.approx((0.7 - 1.0) ** 2, abs=1e-4)


def test_list_forecasts_filters_by_status():
    f1 = _make_forecast(probability=0.5)
    f1_id = db.save_forecast(f1, resolution_source="x")
    f2 = _make_forecast(probability=0.5)
    f2_id = db.save_forecast(f2, resolution_source="x")
    db.resolve_forecast(f2_id, outcome=1.0)

    active = db.list_forecasts(status="active")
    resolved = db.list_forecasts(status="resolved")
    assert {r.id for r in active} == {f1_id}
    assert {r.id for r in resolved} == {f2_id}


def test_mark_refreshed_flagged_sets_review_flag():
    f = _make_forecast()
    fid = db.save_forecast(f, resolution_source="x")

    db.mark_refreshed(fid, flagged=True)

    record = db.get_forecast(fid)
    assert record is not None
    assert record.last_refreshed_at is not None
    assert record.flagged_for_resolution_review is True


def test_active_forecast_ids_excludes_resolved_and_ambiguous():
    f1 = _make_forecast()
    f1_id = db.save_forecast(f1, resolution_source="x")
    f2 = _make_forecast()
    f2_id = db.save_forecast(f2, resolution_source="x")
    db.resolve_forecast(f2_id, outcome=1.0)
    f3 = _make_forecast()
    f3_id = db.save_forecast(f3, resolution_source="x")
    db.resolve_forecast(f3_id, outcome=None)  # ambiguous

    active_ids = set(db.list_active_forecast_ids())
    assert active_ids == {f1_id}
