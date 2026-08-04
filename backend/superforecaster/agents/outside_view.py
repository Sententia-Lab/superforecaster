"""Outside-view agent — principles 4 and 7.

Find reference classes and their base rates BEFORE any case-specific detail is
considered. The graph enforces the ordering; this agent supplies the anchor.
"""

from __future__ import annotations

from config import get_research_limits, resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import Decomposition, ForecastInput, OutsideView
from ..observability import run_agent
from ..tools import search_web, search_wikipedia
from . import as_of_note, format_question, with_model

INSTRUCTIONS = """You establish the OUTSIDE VIEW. You do not produce a final
probability, and you do not reason about what makes this case special — a later step
does that. Your only job is: how often does this kind of thing happen?

FIND REFERENCE CLASSES (principle 4)
A reference class is a population of past cases this question belongs to. For "will
this startup be acquired within 12 months", candidates are: all startups at this
stage, all startups in this sector, all companies this acquirer has approached.

For each class, find the base rate — the fraction of that population where the thing
happened — and record how many cases it is drawn from and where the number came from.
Search for it. A rate you reasoned your way to is not a base rate.

USE AT LEAST TWO CLASSES (principle 7 — dragonfly eye)
One reference class is a single lens and will mislead you. Find at least two that
frame the question differently. Broad and narrow, or by-actor and by-sector.

DISAGREEMENT IS INFORMATION, NOT AN INCONVENIENCE
If your classes give materially different rates — say 12% and 55% — that gap tells you
how uncertain this question really is. Write it in `disagreement`: which class you
trust more for this question and why, and what the spread implies. Do NOT quietly
average them and move on. Leave `disagreement` empty only when the classes broadly
agree.

AGGREGATE
Set `aggregate_base_rate` to your best single anchor across the classes, weighted by
how well each fits this specific question. Say nothing about case specifics — that is
the next step's job, and its adjustments are measured as a delta from your number.

BUDGET
You have a limited search budget. Prefer a few well-chosen searches over exhaustive
looping. If evidence is thin, say so in the class `source` and lower `sample_size`
rather than inventing a rate.
"""


def build_outside_view_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, OutsideView]:
    return Agent[ForecastDeps, OutsideView](
        model=model or resolve_agent_model(),
        name="outside_view_agent",
        deps_type=ForecastDeps,
        output_type=OutsideView,
        system_prompt=INSTRUCTIONS,
        tools=[search_web, search_wikipedia],
        retries=1,
    )


_agent: Agent[ForecastDeps, OutsideView] | None = None


def get_outside_view_agent() -> Agent[ForecastDeps, OutsideView]:
    global _agent
    if _agent is None:
        _agent = build_outside_view_agent()
    return _agent


async def run_outside_view(
    input: ForecastInput,
    decomposition: Decomposition,
    deps: ForecastDeps,
) -> OutsideView:
    """Find reference classes and base rates. Searches; budget-limited."""
    researchable = [
        s.question for s in decomposition.sub_claims if s.knowability == "researchable"
    ]
    focus = (
        "Prioritise base rates for these sub-claims, which were labelled researchable:\n"
        + "\n".join(f"  - {q}" for q in researchable)
        if researchable
        else "No sub-claim was labelled researchable — find the best reference class you "
        "can for the question as a whole, and be explicit about how loose the fit is."
    )

    prompt = f"""Establish the outside view for this question.

{format_question(input)}{as_of_note(deps)}

DECOMPOSITION FROM THE PREVIOUS STEP:
{decomposition.model_dump_json(indent=2)}

{focus}

SEARCH BUDGET: at most {input.max_iterations} rounds. Stop when it is used.

Return an OutsideView with at least two reference classes."""

    agent = get_outside_view_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            usage_limits=get_research_limits(input.max_iterations),
            run_name="outside view",
        )
    return result.output
