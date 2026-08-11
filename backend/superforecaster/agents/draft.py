"""Draft agent — freeform text into a structured question.

Standalone, and deliberately separate from `critic.py`. Extraction ("what did they
actually say?") and adjudicability ("would two people agree on resolution day?") are
different jobs, and folding them into one agent would make `score_critic` measure two
things at once.

This exists because the UI opens with a single textarea. Everything downstream —
`run_critique`, `ForecastInput`, the graph — needs the question, the criteria, and the
date as separate fields, so something has to split them before any of that can run.

It fills all four fields, so "Draft with AI" produces a runnable forecast on its own.
`resolution_source` is the one field it supplies rather than extracts — see ADR 64. Its
budget allows no tool calls, so it names the adjudicator from what the model knows; the
critic, which does search, is what verifies the name behind "Check resolvable".
"""

from __future__ import annotations

from ..config import get_budget, get_model_settings, resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import DraftedQuestion
from ..observability import run_agent
from . import with_model

INSTRUCTIONS = """You convert one block of freeform text into a structured forecasting
question. You do NOT judge it, improve it, or forecast it — another step critiques the
criteria and another produces a probability.

EXTRACT ONLY WHAT IS THERE
The author's intent is the thing to preserve. Do not add a threshold they did not
state, and do not sharpen a vague phrase — a later step is responsible for finding
exactly those gaps, and filling them here would hide them.

`resolution_source` is the one exception. Name one for every question, whether or not
the author did. It is not part of what they said; it is what makes the question
scoreable at all, and leaving it empty stops the forecast from being run.

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
                  The publication, dataset, register, or body whose output settles this
                  on the resolution date. Required for every question. Use the author's
                  source when they named one; name the obvious adjudicator for the
                  subject when they did not.
                  Be specific enough that someone could go and read it — "the ONS
                  Consumer Price Inflation bulletin", not "official statistics"; "the
                  SEC EDGAR full-text filing search", not "public filings".
                  Do not invent a publication you are unsure exists. When nothing
                  standard covers the subject, name the body that would announce the
                  event itself — a company's own press release, a ministry, a court.
                  That is a real adjudicator and it is checkable.
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
