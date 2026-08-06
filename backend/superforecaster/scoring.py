"""Forecast scoring — time-weighted Brier, and calibration by decile.

This is arithmetic over rows, not persistence. It lived in `db.py` on the reasoning that
it is "fundamentally a query against the `forecast_updates` table", but the query is two
lines and the maths is ninety: what a forecast is *worth* is a methodology question, and
keeping it here means it can be tested without a database and read without scrolling past
six tables of DDL.

`db` calls in, never out.
"""

from __future__ import annotations

from datetime import datetime

from .models import CalibrationBucket, CalibrationReport

BUCKETS = 10
"""Deciles. Enough resolution to see a calibration curve bend, few enough that a bucket
has cases in it before the hundredth forecast."""


def time_weighted_probability(
    probabilities: list[float],
    timestamps: list[datetime],
    resolution_date: datetime,
) -> float:
    """Average of every update, weighted by how long it was the standing forecast.

    A forecast held at 20% for eleven months and moved to 90% the day before resolution
    was not a 55% forecast — it was a 20% forecast with a late correction, and the score
    has to say so. Each update is held from its own timestamp until the next one, and the
    final update until resolution.

    Degenerate horizon — everything logged at the resolution date — falls back to the
    last probability, since every weight would be zero.
    """
    if not probabilities:
        raise ValueError("no updates to score")

    boundaries = [*timestamps, resolution_date]
    total = (resolution_date - timestamps[0]).total_seconds()
    if total <= 0:
        return probabilities[-1]

    return sum(
        p * ((boundaries[i + 1] - boundaries[i]).total_seconds() / total)
        for i, p in enumerate(probabilities)
    )


def brier_score(probability: float, outcome: float) -> float:
    """Squared error. Lower is better; 0.25 is what you get by always saying 50%."""
    return (probability - outcome) ** 2


def calibration(
    scored: list[tuple[float, float, float]],
    ambiguous_count: int,
) -> CalibrationReport:
    """Aggregate Brier plus a per-decile calibration curve.

    `scored` is `(probability, outcome, brier)` per resolved, non-ambiguous forecast.

    The top bucket is closed on the right so a forecast at exactly 1.0 lands in 90-100%
    rather than falling off the end — the one boundary case a half-open sweep gets wrong,
    and the one a confident forecaster produces most.
    """
    if not scored:
        return CalibrationReport(
            aggregate_brier_score=None,
            total_resolved=0,
            total_ambiguous_excluded=ambiguous_count,
            buckets=[],
        )

    buckets: list[CalibrationBucket] = []
    for i in range(BUCKETS):
        low, high = i / BUCKETS, (i + 1) / BUCKETS
        in_bucket = [
            (p, o)
            for p, o, _ in scored
            if low <= p < high or (i == BUCKETS - 1 and p == 1.0)
        ]
        if not in_bucket:
            continue
        buckets.append(
            CalibrationBucket(
                range=f"{int(low * 100)}-{int(high * 100)}%",
                predicted_avg=sum(p for p, _ in in_bucket) / len(in_bucket),
                actual_frequency=sum(o for _, o in in_bucket) / len(in_bucket),
                count=len(in_bucket),
            )
        )

    return CalibrationReport(
        aggregate_brier_score=sum(b for _, _, b in scored) / len(scored),
        total_resolved=len(scored),
        total_ambiguous_excluded=ambiguous_count,
        buckets=buckets,
    )
