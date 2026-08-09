"""Draft agent — freeform text into a structured question.

Standalone, and deliberately separate from `critic.py`. Extraction ("what did they
actually say?") and adjudicability ("would two people agree on resolution day?") are
different jobs, and folding them into one agent would make `score_critic` measure two
things at once.

This exists because the UI opens with a single textarea. Everything downstream —
`run_critique`, `ForecastInput`, the graph — needs the question, the criteria, and the
date as separate fields, so something has to split them before any of that can run.
"""

from __future__ import annotations

from config import get_budget, get_model_settings, resolve_agent_model
from pydantic_ai import Agent
from superforecaster.tools import search_web

from ..deps import ForecastDeps
from ..models import DraftedQuestion
from ..observability import run_agent
from . import with_model

INSTRUCTIONS = """You convert one block of freeform text into a structured forecasting
question. You do NOT judge it, improve it, or forecast it — another step critiques the
criteria and another produces a probability.

EXTRACT ONLY WHAT IS THERE
The author's intent is the thing to preserve. Do not add a threshold they did not
state, do not name a resolution source they did not name, and do not sharpen a vague
phrase — a later step is responsible for finding exactly those gaps, and filling them
here would hide them.

FIELDS
  question        One interrogative sentence, ending in a question mark. If the text
                  is phrased as a statement ("Anthropic will IPO in 2026"), turn it
                  into the matching question without changing its meaning.
  resolution_criteria
                  What the author said counts as YES, in their words where possible.
                  If they said nothing about resolution, restate the question as a
                  bare condition rather than inventing criteria.
  resolution_date The date the question settles. Resolve relative language ("end of
                  next year", "within 18 months") against the current date given
                  below. If the text gives no date at all, use the most defensible
                  reading of its horizon — that guess is then visible for the author
                  to correct.
  category        One lowercase word: finance, economics, politics, ai, energy,
                  science, health, sport, tech, or general.
  resolution_source
                  Only when the text names one. Empty string otherwise — an invented
                  source is worse than a missing one, because the next step is
                  looking for exactly this gap.
"""


def build_draft_agent(model: str | None = None) -> Agent[ForecastDeps, DraftedQuestion]:
    return Agent[ForecastDeps, DraftedQuestion](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
        name="draft",
        deps_type=ForecastDeps,
        output_type=DraftedQuestion,
        system_prompt=INSTRUCTIONS,
        retries=1,
        tools=[search_web],
    )


_agent: Agent[ForecastDeps, DraftedQuestion] | None = None


def get_draft_agent() -> Agent[ForecastDeps, DraftedQuestion]:
    global _agent
    if _agent is None:
        _agent = build_draft_agent()
    return _agent


async def run_draft(text: str, deps: ForecastDeps | None = None) -> DraftedQuestion:
    """Split freeform text into the fields a forecast needs. No tools, one call."""
    deps = deps or ForecastDeps()
    today = (deps.as_of or _utc_now()).date().isoformat()

    prompt = f"""Extract a structured question from this text.

TODAY'S DATE: {today}

TEXT AS WRITTEN:
{text}

Return a DraftedQuestion."""

    agent = get_draft_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            budget=get_budget(agent.name),
            run_name="draft question",
        )
    return result.output


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
