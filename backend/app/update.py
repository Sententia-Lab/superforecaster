"""The update cycle, wired to storage.

`superforecaster.update` decides what should happen to a forecast. This decides where
the record comes from and what gets written when the answer arrives.
"""

from __future__ import annotations

from superforecaster.deps import ForecastDeps
from superforecaster.models import UpdateOutcome
from superforecaster.update import run_update_cycle

from . import db


async def run_update_graph(forecast_id: str, *, verbose: bool = False) -> UpdateOutcome:
    """Run the daily cycle on one saved forecast. Callable from cron, the API, or the CLI.

    `mark_refreshed` runs whatever the outcome, including a flagged one — the forecast
    was looked at, which is what the timestamp records.
    """
    record = db.get_forecast(forecast_id)
    if record is None:
        return UpdateOutcome(reason="forecast not found")
    if record.outcome is not None or record.is_ambiguous:
        return UpdateOutcome(reason="forecast already resolved")

    outcome = await run_update_cycle(record, ForecastDeps(verbose=verbose))

    db.mark_refreshed(record.id, flagged=outcome.flagged_resolved)
    if outcome.updated and outcome.new_probability is not None:
        db.add_forecast_update(
            forecast_id=record.id,
            probability=outcome.new_probability,
            reasoning=outcome.reasoning,
        )
    return outcome
