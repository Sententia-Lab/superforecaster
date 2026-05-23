"""Refresh agent — decides whether to update an existing forecast's probability.

Runs separately from the resolution agent. By the time refresh runs on a
forecast, the resolution agent has already cleared it (not flagged for
review). This agent's only job is: given new evidence in the last
SEARCH_LOOKBACK_HOURS hours, should the probability move?

Threshold-based: a probability change of less than MIN_PROBABILITY_DELTA
is treated as noise and not written. The agent itself produces the
should_update signal; the orchestrator enforces the threshold.
"""

from __future__ import annotations

from config import get_settings, resolve_agent_model
from pydantic_ai import Agent

from . import db
from .models import (
    ForecastRecord,
    ForecastRefreshResult,
    RefreshActionResponse,
)
from .observability import run_agent
from .tools import search_web, search_wikipedia


SYSTEM_PROMPT = """You are reviewing an existing probability forecast for an UPDATE.

You are NOT producing a fresh forecast. You are deciding whether substantive
new evidence has emerged that warrants moving the probability.

PROCESS
1. Search for news on the question over the lookback window provided.
2. Compare new evidence against the current probability and the full
   update history given to you.
3. Set should_update = true ONLY if there is genuinely new, substantive
   information. Otherwise should_update = false.

DO NOT UPDATE FOR:
- Restating prior evidence in different words
- Absence of news (no news is not evidence)
- Marginal probability movements you cannot defend with specific facts
- Rephrasing the same view

WHEN YOU UPDATE
- Set new_probability with granularity (e.g. 0.62, not 0.60).
- Set new_confidence based on the strength of new evidence.
- List the specific pieces of evidence in evidence_found.
- Explain in reasoning what changed and why this evidence justifies the move.

WHEN YOU DO NOT UPDATE
- Set new_probability = null, new_confidence = null.
- Briefly explain in reasoning why no change is warranted.
"""


def build_refresh_agent(
    model: str | None = None,
) -> Agent[None, ForecastRefreshResult]:
    return Agent[None, ForecastRefreshResult](
        model=model or resolve_agent_model(),
        name="refresh_agent",
        output_type=ForecastRefreshResult,
        system_prompt=SYSTEM_PROMPT,
        tools=[search_web, search_wikipedia],
        retries=1,
    )


_refresh_agent: Agent[None, ForecastRefreshResult] | None = None


def get_refresh_agent() -> Agent[None, ForecastRefreshResult]:
    global _refresh_agent
    if _refresh_agent is None:
        _refresh_agent = build_refresh_agent()
    return _refresh_agent


def _format_history(record: ForecastRecord) -> str:
    lines = []
    for i, u in enumerate(record.updates, 1):
        late = " [LATE]" if u.is_late else ""
        lines.append(
            f"{i}. {u.created_at.isoformat()} — p={u.probability:.3f} "
            f"({u.confidence}){late}\n   {u.reasoning}"
        )
    return "\n".join(lines) if lines else "(no updates)"


async def run_refresh_agent(record: ForecastRecord, *, verbose: bool = False) -> ForecastRefreshResult:
    """Run the refresh agent on a forecast record. Returns its raw decision."""
    lookback_hours = get_settings().search_lookback_hours
    current_probability = record.updates[-1].probability if record.updates else 0.5

    user_prompt = f"""Decide whether to update this probability forecast.

QUESTION: {record.question}

RESOLUTION CRITERIA: {record.resolution_criteria}

RESOLUTION DATE: {record.resolution_date.isoformat()}

CATEGORY: {record.category}

CURRENT PROBABILITY: {current_probability:.3f}

UPDATE HISTORY:
{_format_history(record)}

LOOKBACK: search for news from the last {lookback_hours} hours.

Return a ForecastRefreshResult."""

    result = await run_agent(
        get_refresh_agent(),
        user_prompt,
        verbose=verbose,
        run_name="refresh run",
    )
    return result.output


async def refresh_forecast(forecast_id: str) -> RefreshActionResponse:
    """Run the refresh cycle on a single forecast.

    Skips already-resolved, ambiguous, or resolution-flagged forecasts.
    Writes a new update only if the agent says yes AND the change exceeds
    MIN_PROBABILITY_DELTA. Always updates last_refreshed_at.
    """
    record = db.get_forecast(forecast_id)
    if record is None:
        return RefreshActionResponse(updated=False, reason="forecast not found")
    if record.outcome is not None or record.is_ambiguous:
        return RefreshActionResponse(updated=False, reason="forecast already resolved")
    if record.flagged_for_resolution_review:
        return RefreshActionResponse(
            updated=False,
            reason="forecast flagged for resolution review; skipping probability update",
        )

    result = await run_refresh_agent(record)
    db.mark_refreshed(forecast_id, flagged=False)

    if not result.should_update or result.new_probability is None:
        return RefreshActionResponse(updated=False, reason=result.reasoning)

    current_probability = record.updates[-1].probability
    threshold = get_settings().min_probability_delta
    if abs(result.new_probability - current_probability) < threshold:
        return RefreshActionResponse(
            updated=False,
            reason=f"agent suggested update but delta < threshold ({threshold:.2f})",
        )

    update = db.add_forecast_update(
        forecast_id=forecast_id,
        probability=result.new_probability,
        confidence=result.new_confidence or "medium",
        reasoning=result.reasoning,
    )
    return RefreshActionResponse(updated=True, update=update)
