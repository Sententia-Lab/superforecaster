"""Resolution agent — decides if a forecast has already resolved.

Runs FIRST in the daily refresh cycle. If it flags a forecast, the
refresh agent skips it. The agent never auto-resolves: it only sets
`flagged_for_resolution_review=True` for the admin to confirm.

Conservative by design — high stakes if wrong (closes a forecast
permanently). The system prompt biases toward "not resolved" in the
face of uncertainty.
"""

from __future__ import annotations

from config import resolve_agent_model
from pydantic_ai import Agent

from . import db
from .models import ForecastRecord, ResolutionCheckResult
from .observability import run_agent
from .tools import search_web, search_wikipedia


SYSTEM_PROMPT = """You are checking whether a forecast question has ALREADY RESOLVED.

This is a binary classification task. You are not estimating probability.
You are deciding: based on available evidence, has the underlying event
described in resolution_criteria definitively occurred (outcome=1.0) or
definitively NOT occurred (outcome=0.0)?

CRITICAL RULES
1. Match evidence against the EXACT resolution_criteria, not the question
   text. The criteria define the bar.
2. Set appears_resolved = true ONLY if you find unambiguous evidence
   meeting the criteria. When in doubt, set appears_resolved = false.
3. NEVER infer resolution from absence of news. "I didn't find anything"
   is not evidence of non-resolution unless the resolution_date has passed.
4. Cite specific evidence in resolution_evidence (URLs, source names, or
   direct quotes from your search results).
5. Default to confidence = "low" unless evidence is overwhelming.

A wrong "appears_resolved=true" is high-cost — it closes a forecast.
A wrong "appears_resolved=false" is low-cost — the forecast simply gets
re-checked tomorrow.

PROCESS
1. Read resolution_criteria carefully.
2. Search for direct evidence the criteria has been met (or definitively
   cannot be met before the resolution_date).
3. If evidence is found, set appears_resolved=true, suggested_outcome
   (0.0 or 1.0), confidence, and resolution_evidence.
4. Otherwise set appears_resolved=false and explain in reasoning what
   you searched for and what you found (or did not find).
"""


def build_resolution_agent(
    model: str | None = None,
) -> Agent[None, ResolutionCheckResult]:
    return Agent[None, ResolutionCheckResult](
        model=model or resolve_agent_model(),
        name="resolution_agent",
        output_type=ResolutionCheckResult,
        system_prompt=SYSTEM_PROMPT,
        tools=[search_web, search_wikipedia],
        retries=1,
    )


_resolution_agent: Agent[None, ResolutionCheckResult] | None = None


def get_resolution_agent() -> Agent[None, ResolutionCheckResult]:
    global _resolution_agent
    if _resolution_agent is None:
        _resolution_agent = build_resolution_agent()
    return _resolution_agent


async def run_resolution_agent(record: ForecastRecord, *, verbose: bool = False) -> ResolutionCheckResult:
    """Run the resolution agent on a forecast record. Returns its raw decision."""
    user_prompt = f"""Determine if this forecast question has already resolved.

QUESTION: {record.question}

RESOLUTION CRITERIA (this is the bar — match evidence against this exactly):
{record.resolution_criteria}

RESOLUTION SOURCE: {record.resolution_source}

RESOLUTION DATE (event must resolve by this date): {record.resolution_date.isoformat()}

CATEGORY: {record.category}

Search for evidence that the criteria has been met or definitively
cannot be met. Return a ResolutionCheckResult.
"""
    result = await run_agent(
        get_resolution_agent(),
        user_prompt,
        verbose=verbose,
        run_name="resolution run",
    )
    return result.output


async def check_resolution(forecast_id: str) -> ResolutionCheckResult:
    """Check resolution for one forecast. Flags the forecast if appears_resolved.

    Skips already-resolved or ambiguous forecasts. Always updates
    last_refreshed_at. Never auto-resolves — admin confirms.
    """
    record = db.get_forecast(forecast_id)
    if record is None:
        raise db.NotFoundError(f"forecast {forecast_id}")
    if record.outcome is not None or record.is_ambiguous:
        # Skip; nothing to mark
        return ResolutionCheckResult(
            appears_resolved=False,
            confidence="low",
            reasoning="forecast already resolved; skipped",
        )

    result = await run_resolution_agent(record)
    db.mark_refreshed(forecast_id, flagged=result.appears_resolved)
    return result
