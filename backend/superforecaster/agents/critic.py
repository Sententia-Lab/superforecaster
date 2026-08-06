"""Criteria critic — principle 3.

Standalone: not part of the forecast graph. It runs before a question is forecast at
all, which is the only time fixing the criteria is cheap.

"Ambiguity here silently corrupts everything downstream" — a question whose criteria
cannot be adjudicated produces a forecast that cannot be scored, and the problem is
invisible until resolution day. This agent is the frontend's suggestion box.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from config import get_critique_budget, get_critique_limits, resolve_agent_model
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from ..deps import ForecastDeps, SearchBudget
from ..models import CriteriaCritique
from ..observability import run_agent
from ..tools import search_web
from . import attach_budget_pressure, with_model

INSTRUCTIONS = """You review forecast questions for resolvability. You do not forecast
them — you decide whether they COULD be scored fairly once the date arrives.

THE TEST
Imagine two people who disagree about the outcome, both reading these criteria on the
resolution date with all the facts in front of them. Would they reach the same verdict?
If they could argue, the criteria are not resolvable yet.

WHAT MAKES CRITERIA FAIL
  Vague predicates      "significant", "major", "widely adopted", "successful"
                        — replace with a number and a unit.
  No source             who publishes the number that settles this? Name it.
  No threshold          "growth" vs "growth of at least 10%".
  Ambiguous timing      no timezone, "by end of year" without a date, unclear whether
                        the event or its announcement counts.
  Undefined subject     "the top 3 schools" — by what measure, ranked when?
  Compound conditions   two things joined by "and"/"or" without saying which governs.
  Unfalsifiable         no observable event would settle it either way.

WHAT TO RETURN
  is_resolvable  false if you found anything above that two reasonable people could
                 argue over. Be strict — this is the cheap moment to fix it.
  ambiguities    each specific phrase that could be read two ways, quoted.
  missing        structural gaps: no resolution source, no timezone, no threshold.
  suggested_criteria
                 a rewritten version that is actually adjudicable. Keep the author's
                 intent; change only what has to change. Name a source, give numbers
                 and units, and state the exact observable event that counts as yes.
  suggested_resolution_source
                 the specific publication, dataset, or body that would settle it.

You may search to check whether a named source exists and publishes what the criteria
assume it does — a criterion resting on a statistic nobody publishes is not resolvable.
That is the only thing worth searching for here. You are judging the wording of the
question, not forecasting it: do not go looking for the answer, for background on the
topic, or for a better source than the one you already found. A handful of searches is
the whole budget, and every tool result tells you what is left of it.
"""


def build_critic_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, CriteriaCritique]:
    agent = Agent[ForecastDeps, CriteriaCritique](
        model=model or resolve_agent_model(),
        name="critic_agent",
        deps_type=ForecastDeps,
        output_type=CriteriaCritique,
        system_prompt=INSTRUCTIONS,
        tools=[search_web],
        retries=1,
    )
    attach_budget_pressure(agent)
    return agent


_agent: Agent[ForecastDeps, CriteriaCritique] | None = None


def get_critic_agent() -> Agent[ForecastDeps, CriteriaCritique]:
    global _agent
    if _agent is None:
        _agent = build_critic_agent()
    return _agent


async def run_critique(
    question: str,
    resolution_criteria: str,
    resolution_date: datetime | None = None,
    deps: ForecastDeps | None = None,
) -> CriteriaCritique:
    """Evaluate whether a question could be scored fairly, and suggest a fix."""
    deps = deps or ForecastDeps()
    date_line = (
        f"\nPROPOSED RESOLUTION DATE: {resolution_date.isoformat()}"
        if resolution_date is not None
        else "\nPROPOSED RESOLUTION DATE: (none given — that is itself a finding)"
    )

    prompt = f"""Review this forecast question for resolvability.

QUESTION: {question}

PROPOSED RESOLUTION CRITERIA:
{resolution_criteria}{date_line}

Return a CriteriaCritique."""

    # The critic owns its budget rather than inheriting one: it is standalone, never a
    # cell in a fanned-out row, so there is no caller whose budget it should share.
    soft, hard = get_critique_budget()
    deps = replace(deps, budget=SearchBudget(soft_depth=soft, hard_depth=hard))

    agent = get_critic_agent()
    try:
        with with_model(agent, deps) as bound:
            result = await run_agent(
                bound,
                prompt,
                deps=deps,
                verbose=deps.verbose,
                usage_limits=get_critique_limits(),
                run_name="criteria critique",
            )
    except UsageLimitExceeded:
        return _unfinished(resolution_criteria)
    return result.output


def _unfinished(resolution_criteria: str) -> CriteriaCritique:
    """What the critic returns when it hit the wall instead of converging.

    `UsageLimitExceeded` is raised before the next tool runs, so there is no partial
    critique to salvage — but there is a parsed question, and `/questions/draft` returns
    both from one call. Raising here 500s that endpoint and the frontend drops the user
    back to an empty draft box, losing text they just typed. Degrading costs them a
    dismiss click instead.

    `is_resolvable=False` is what surfaces this in the UI at all — the suggestion box is
    hidden when it is true — and it claims only that the check did not clear the
    criteria, which is exactly what happened. The rewrite is the author's own text
    unchanged, so applying it is a no-op rather than a fabricated suggestion.
    """
    return CriteriaCritique(
        is_resolvable=False,
        ambiguities=[],
        missing=[
            "The resolvability check ran out of search budget before it reached a "
            "verdict. Nothing is known to be wrong with these criteria — they are "
            "simply unreviewed. Dismiss to proceed, or edit and re-run the check."
        ],
        suggested_criteria=resolution_criteria,
        suggested_resolution_source="",
    )
