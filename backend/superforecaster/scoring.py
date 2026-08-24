"""Forecast scoring — time-weighted Brier, and calibration by decile."""

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
    """Average of every update, weighted by how long it was the standing forecast."""
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
    """Aggregate Brier plus a per-decile calibration curve."""
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
