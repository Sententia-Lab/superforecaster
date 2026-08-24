"""The daily update cycle. Reads nothing and writes nothing; `app.update` owns
storage."""

from __future__ import annotations

from . import checks
from .agents.resolution import run_resolution_check
from .agents.update import run_update
from .config import get_check_thresholds
from .deps import ForecastDeps
from .models import ForecastRecord, UpdateOutcome


async def run_update_cycle(
    record: ForecastRecord, deps: ForecastDeps | None = None
) -> UpdateOutcome:
    """Check one forecast for resolution, then update it against new evidence."""
    deps = deps or ForecastDeps()
    thresholds = get_check_thresholds()

    resolution = await run_resolution_check(record, deps)
    if resolution.appears_resolved:
        return UpdateOutcome(
            flagged_resolved=True,
            reason="flagged for resolution review; probability update skipped — "
            f"{resolution.reasoning}",
        )

    decision = await run_update(record, deps)
    if checks.is_large_move(decision, thresholds):
        # A large move is verified, not capped (ADR 16). Once only.
        revised = await run_update(
            record, deps, verify=(decision.prior, decision.posterior)
        )
        decision = revised.model_copy(update={"verified_large_move": True})

    violations = checks.run_update_checks(decision, thresholds)
    delta = abs(decision.posterior - decision.prior)
    if delta < thresholds.min_probability_delta:
        return UpdateOutcome(
            violations=violations,
            reason=f"change of {delta:.3f} is below the "
            f"{thresholds.min_probability_delta:.3f} threshold — treated as noise",
        )
    if checks.blocking(violations):
        names = ", ".join(v.name for v in violations)
        return UpdateOutcome(
            new_probability=decision.posterior,
            violations=violations,
            reason=f"update is internally inconsistent ({names}); not written",
        )
    return UpdateOutcome(
        updated=True,
        new_probability=decision.posterior,
        violations=violations,
        reason=f"probability moved {decision.prior:.3f} -> {decision.posterior:.3f}",
        reasoning=decision.reasoning,
    )
