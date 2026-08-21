"""The update cycle, wired to storage."""

from __future__ import annotations

from superforecaster.deps import ForecastDeps
from superforecaster.models import UpdateOutcome
from superforecaster.update import run_update_cycle

from . import db, research


async def run_update_graph(forecast_id: str) -> UpdateOutcome:
    """Run the daily cycle on one saved forecast. Callable from cron, the API, or the
    CLI."""
    record = db.get_forecast(forecast_id)
    if record is None:
        return UpdateOutcome(reason="forecast not found")
    if record.outcome is not None or record.is_ambiguous:
        return UpdateOutcome(reason="forecast already resolved")

    outcome = await run_update_cycle(
        record, ForecastDeps(store=research.store_for(record.research_id))
    )

    db.mark_refreshed(record.id, flagged=outcome.flagged_resolved)
    if outcome.updated and outcome.new_probability is not None:
        db.add_forecast_update(
            forecast_id=record.id,
            probability=outcome.new_probability,
            reasoning=outcome.reasoning,
        )
    return outcome
