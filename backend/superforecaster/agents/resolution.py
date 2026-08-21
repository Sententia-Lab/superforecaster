"""Resolution agent — has this question already resolved?

Runs first in the daily cycle. If it flags a forecast, the update step is skipped
entirely. It never auto-resolves: it only raises a flag for an admin to confirm.

Conservative by design, because the errors are asymmetric. A wrong "not resolved" is
cheap — the forecast gets re-checked tomorrow. A wrong "resolved" closes a forecast
permanently. The prompt is written to bias toward the cheap error.

Prompt text ported from the previous `superforecaster/resolution.py`.
"""

from __future__ import annotations

from ..config import get_budget, get_model_settings, resolve_agent_model
from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks

from ..deps import ForecastDeps
from ..models import ForecastRecord, ResolutionCheckResult
from ..runner import run_agent
from ..tools import (
    crawl_site,
    extract_pages,
    map_site,
    search_research,
    search_web,
    search_wikipedia,
)
from . import with_model, withdraw_tools

INSTRUCTIONS = """You are checking whether a forecast question has ALREADY RESOLVED.

This is a binary classification task. You are not estimating a probability. You are
deciding: based on available evidence, has the event described in resolution_criteria
definitively occurred (outcome=1.0) or definitively NOT occurred (outcome=0.0)?

CRITICAL RULES
1. Match evidence against the EXACT resolution_criteria, not the question text. The
   criteria define the bar.
2. Set appears_resolved = true ONLY on unambiguous evidence meeting those criteria.
   When in doubt, set it false.
3. NEVER infer resolution from absence of news. "I found nothing" is not evidence of
   non-resolution unless the resolution_date has passed.
4. Cite specific evidence in resolution_evidence — URLs, source names, or direct
   quotes from your search results.
5. Default to confidence = "low" unless the evidence is overwhelming.

A wrong "appears_resolved=true" is high-cost: it closes a forecast.
A wrong "appears_resolved=false" is low-cost: it gets re-checked tomorrow.

PROCESS
1. Read resolution_criteria carefully.
2. Call `search_research` first — the pages this forecast was built on may already name
   the source the criteria point at, and reading one costs no network call.
3. Search for direct evidence the criteria has been met, or definitively cannot be
   met before the resolution_date.
4. If found, set appears_resolved=true with suggested_outcome, confidence, and
   resolution_evidence.
5. Otherwise set appears_resolved=false and explain what you searched for and what
   you did and did not find.
"""


def build_resolution_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, ResolutionCheckResult]:
    return Agent[ForecastDeps, ResolutionCheckResult](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
        name="resolution",
        deps_type=ForecastDeps,
        output_type=ResolutionCheckResult,
        system_prompt=INSTRUCTIONS,
        tools=[
            search_research,
            search_web,
            extract_pages,
            crawl_site,
            map_site,
            search_wikipedia,
        ],
        capabilities=[Hooks(prepare_tools=withdraw_tools)],
        retries=1,
    )


_agent: Agent[ForecastDeps, ResolutionCheckResult] | None = None


def get_resolution_agent() -> Agent[ForecastDeps, ResolutionCheckResult]:
    global _agent
    if _agent is None:
        _agent = build_resolution_agent()
    return _agent


async def run_resolution_check(
    record: ForecastRecord, deps: ForecastDeps
) -> ResolutionCheckResult:
    """Ask whether this forecast has already resolved. Never closes it."""
    prompt = f"""Determine if this forecast question has already resolved.

QUESTION: {record.question}

RESOLUTION CRITERIA (this is the bar — match evidence against this exactly):
{record.resolution_criteria}

RESOLUTION SOURCE: {record.resolution_source}

RESOLUTION DATE (event must resolve by this date): {record.resolution_date.isoformat()}

CATEGORY: {record.category}

Search for evidence the criteria has been met or definitively cannot be met.
Return a ResolutionCheckResult."""

    agent = get_resolution_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            budget=get_budget(agent.name),
            run_name="resolution check",
        )
    return result.output
