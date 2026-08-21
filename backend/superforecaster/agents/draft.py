"""Draft agent — freeform text into the four fields a forecast needs. No tools."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic_ai import Agent

from ..config import get_budget
from ..deps import ForecastDeps
from ..models import DraftedQuestion
from ..runner import run_agent

INSTRUCTIONS = """You convert one block of freeform text into a structured forecasting
question. You do not judge, improve, or forecast it.

Extract only what is there. Do not add a threshold the author did not state or sharpen
a vague phrase — a later step finds those gaps. The one exception is
`resolution_source`: name one for every question, because without it the forecast
cannot be scored.

  question            One interrogative sentence ending in a question mark.
  resolution_criteria What the author said counts as YES, in their words where possible.
  resolution_date     When the question settles. Resolve "end of next year" against
                      today's date. If no date is given, take the most defensible
                      reading of the horizon.
  category            One lowercase word: finance, economics, politics, ai, energy,
                      science, health, sport, tech, or general.
  resolution_source   The publication, dataset, register, or body whose output settles
                      this — "the ONS Consumer Price Inflation bulletin", not "official
                      statistics". When nothing standard covers it, name the body that
                      would announce the event itself."""

agent = Agent[ForecastDeps, DraftedQuestion](
    name="draft",
    deps_type=ForecastDeps,
    output_type=DraftedQuestion,
    instructions=INSTRUCTIONS,
    retries=1,
)


async def run_draft(text: str, deps: ForecastDeps | None = None) -> DraftedQuestion:
    today = datetime.now(timezone.utc).date().isoformat()
    prompt = f"Extract a structured question from this text.\n\nTODAY'S DATE: {today}\n\nTEXT AS WRITTEN:\n{text}"
    result = await run_agent(
        agent,
        prompt,
        deps=deps or ForecastDeps(),
        budget=get_budget("draft"),
        run_name="draft question",
    )
    return result.output
