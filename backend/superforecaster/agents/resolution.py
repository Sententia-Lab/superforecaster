"""Resolution agent — has this question already resolved? It only raises a flag; a
wrong "resolved" closes a forecast, a wrong "not resolved" costs a day."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks

from ..config import get_budget
from ..deps import ForecastDeps
from ..models import ForecastRecord, ResolutionCheckResult
from ..runner import run_agent
from ..tools import extract_pages, search_research, search_web, search_wikipedia
from . import withdraw_tools

INSTRUCTIONS = """You decide whether a forecast question has ALREADY RESOLVED. This is a
binary classification, not a probability estimate.

1. Match evidence against the EXACT resolution criteria, not the question text.
2. Set `appears_resolved` true ONLY on unambiguous evidence. When in doubt, false.
3. Never infer resolution from absence of news unless the resolution date has passed.
4. Cite specific evidence — URLs, source names, or quotes — in `resolution_evidence`.
5. Default `confidence` to "low" unless the evidence is overwhelming.

A wrong "resolved" closes a forecast; a wrong "not resolved" is re-checked tomorrow.
Search for direct evidence the criteria have been met, or definitively cannot be met
before the resolution date. Otherwise explain what you searched for and did not find."""

agent = Agent[ForecastDeps, ResolutionCheckResult](
    name="resolution",
    deps_type=ForecastDeps,
    output_type=ResolutionCheckResult,
    instructions=INSTRUCTIONS,
    tools=[search_research, search_web, extract_pages, search_wikipedia],
    capabilities=[Hooks(prepare_tools=withdraw_tools)],
    retries=1,
)


async def run_resolution_check(
    record: ForecastRecord, deps: ForecastDeps
) -> ResolutionCheckResult:
    prompt = f"""Determine if this forecast question has already resolved.

QUESTION: {record.question}

RESOLUTION CRITERIA (match evidence against this exactly):
{record.resolution_criteria}

RESOLUTION SOURCE: {record.resolution_source}

RESOLUTION DATE: {record.resolution_date.isoformat()}

CATEGORY: {record.category}"""
    result = await run_agent(
        agent,
        prompt,
        deps=deps,
        budget=get_budget("resolution"),
        run_name="resolution check",
    )
    return result.output
