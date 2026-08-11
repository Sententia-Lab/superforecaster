"""Outside-view agents — principle 4.

Measure ONE population that `agents.lenses` already named, before any case-specific
detail is considered. The graph enforces the ordering; these agents supply the anchor.

**Rates are counted, never stated.** The agent returns evidence blocks — cases it
enumerated, statistics someone else published — and `checks.lens_rate` divides. That is
the difference between "70%" and "7 of the 10 cases I found did, and here they are", and
it is what `check_base_rate_derivation` audits.

One agent per *lens*, not per sub-question: with three lenses on five sub-questions the
research fans out fifteen ways. The fan-out itself is a `.map()` edge in
`graphs.forecast`, so this module knows nothing about how many cells run or when.
"""

from __future__ import annotations

from dataclasses import replace

from config import get_budget, get_model_settings, resolve_agent_model
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from .. import checks
from ..deps import ForecastDeps
from ..models import (
    Decomposition,
    Evidence,
    ForecastInput,
    Lens,
    OutsideView,
    ResearchedLens,
    SubQuestionBaseRates,
    SubPrediction,
)
from ..observability import run_agent
from ..tools import search_web, search_wikipedia
from . import (
    as_of_note,
    attach_budget,
    format_question,
    withdraw_spent_tools,
    with_model,
)

INSTRUCTIONS = """You measure ONE population and report what you counted. You do not forecast.

COUNT, NEVER ESTIMATE
Every number comes from cases you found or a statistic someone else published. `hits`
and `n` are counts. A rate you reasoned your way to is not a base rate.

SEARCH IN THIS ORDER
Stop at the first step that gives you an evidence block.

  1. A published statistic for this population. One search — the cheapest full answer.
  2. A page listing many cases at once: an index, table, survey, or "list of" article.
     One or two searches. Take every case from that one page.
  3. Individual cases, one search each, at most three. Do not chase a tenth case.

Never repeat a search with reworded terms. A query that found nothing means this
population is hard to measure, not that the words were wrong.

WHEN NOTHING MEASURES THIS POPULATION EXACTLY
This is the common case, not the failure case. Populations here are written to be
precise, and precise ones are often narrower than anything anybody has published.

Measure the nearest population somebody *has* measured, and grade it down. A wider
class, an older window, a partial sample — any of these is a real base rate. Name the
gap in `note` and in `disagreement`, so the next step knows what it is standing on.

  the population   midterms since 1946 where the out-party led the generic ballot
                   by 3+ points in the final 60 days
  no published measure of exactly that
  good             all post-war midterms for the president's party, graded `medium`,
                   with the gap named: "not conditioned on the polling lead"

The one thing that is never right is to keep searching for an exact match that does not
exist. Two searches that return nothing new mean you are already at this step.

STOP AND WRITE UP
Stop at whichever comes first:
  - you hold a published block, or a counted block with 3 or more named cases;
  - two searches in a row return nothing new;
  - two searches remain.

You will be stopped anyway: the search tools are withdrawn once the budget is spent, and
whatever you hold at that moment becomes the answer. Landing early and deliberately, on
the nearest measurable population, beats being cut off mid-count.

GRADE EACH SOURCE FOR THIS RATE, NOT FOR ITS REPUTATION
  high    a dataset or study measuring this population directly
  medium  relevant but indirect — adjacent population, older data, partial coverage
  low     a single report, a secondhand figure, or a number you had to infer

Most honest answers here are `medium`. Say in `note` what makes it that.

A counted block must name every case in `analogs`, because a check compares that list
against `n` and `hits`. Three cases you can name beat ten you cannot.
"""


def build_base_rate_cell_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, SubQuestionBaseRates]:
    """One column's base-rate researcher."""
    agent = Agent[ForecastDeps, SubQuestionBaseRates](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
        name="base_rate_cell_agent",
        deps_type=ForecastDeps,
        output_type=SubQuestionBaseRates,
        system_prompt=INSTRUCTIONS,
        tools=[search_web, search_wikipedia],
        prepare_tools=withdraw_spent_tools,
        retries=1,
    )
    attach_budget(agent)
    return agent


_cell_agent: Agent[ForecastDeps, SubQuestionBaseRates] | None = None


def get_base_rate_cell_agent() -> Agent[ForecastDeps, SubQuestionBaseRates]:
    global _cell_agent
    if _cell_agent is None:
        _cell_agent = build_base_rate_cell_agent()
    return _cell_agent


def cell_deps(deps: ForecastDeps, sub_question_id: str) -> ForecastDeps:
    """A deps copy bound to one column, with its own source list.

    The private `sources_seen` is not a style choice: `observability` detects new sources
    by remembering how long that list was and slicing off the tail, and two cells
    appending to one list makes that index hand each cell the other's sources. The parent
    extends from these after the barrier.

    The budget is not set here. `run_agent` attaches it, so a cell's tag and a cell's
    ceilings have one owner each rather than sharing this function.
    """
    return replace(deps, sub_question=sub_question_id, sources_seen=[])


async def run_research_lens(
    input: ForecastInput,
    sub_question: SubPrediction,
    lens: Lens,
    deps: ForecastDeps,
) -> SubQuestionBaseRates:
    """Measure exactly one population. Searches; budget-limited."""
    prompt = f"""Measure ONE population.

{format_question(input)}{as_of_note(deps)}

THE PART OF THE QUESTION THIS BEARS ON — {sub_question.id}: {sub_question.question}

YOUR POPULATION — {lens.name}
Who is in it: {lens.population}
Why it was chosen: {lens.why_it_fits}

Find historical data on this question, published or counted, for this population. Aim at
it and nothing else, and do not weigh it against any other lens — that is already decided.

If nothing measures it exactly, measure the nearest population that somebody has measured
and say so. That is a graded-down base rate, not a substitution: the boundary you report
is the one you actually counted, and `note` and `disagreement` say how it differs from the
population above. What is forbidden is quietly swapping in an easier population and
reporting it as this one.

Return a SubQuestionBaseRates whose `lens` repeats the population exactly as given and adds
your evidence blocks and analogs."""

    agent = get_base_rate_cell_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            budget=get_budget("base_rate_cell", max_iterations=input.max_iterations),
            run_name=f"base rates · {sub_question.id} · {lens.name}",
        )
    return result.output


def exhausted_notice(deps: ForecastDeps) -> None:
    """Say on the wire that a cell blew one of its ceilings.

    `UsageLimitExceeded` is raised *before* the tools run, so by the time it reaches a
    caller there is no output to salvage — the cell contributes nothing and the column
    falls back to its own working estimate in `checks.chain_inputs`. The run continues.

    That fallback is why one greedy column no longer costs the other three their work,
    which is the whole point of moving the budget from the row to the cell.
    """
    if deps.emit is None:
        return
    deps.emit("exhausted", {"id": deps.sub_question}, deps.sub_question)


async def _whole_question_cell(
    input: ForecastInput, decomposition: Decomposition, deps: ForecastDeps
) -> OutsideView:
    """The fallback when no lens was researched for any sub-question.

    Reachable when the decomposition labelled nothing researchable — which
    `check_decomposition`'s P2 arm already discourages — so this is close to a fixture-only
    path. It must not crash, and it is the one remaining producer of a lens naming no
    sub-question.

    Uses the same per-lens cell as everything else, on a synthetic whole-question lens, so
    there is one way to measure a population rather than two that can drift.
    """
    whole = Lens(
        name="the question as a whole",
        population=f"Cases comparable to: {input.question}",
        why_it_fits="No sub-question carried a researchable population, so the fallback "
        "measures the question directly.",
        weight=1.0,
        weight_rationale="The only lens available.",
    )
    fallback_claim = SubPrediction(
        question=input.question,
        probability=0.5,
        rationale="whole-question fallback",
        knowability="researchable",
    )
    result = await run_research_lens(input, fallback_claim, whole, deps)
    view = OutsideView(
        lenses=[result.lens.model_copy(update={"sub_question_ids": []})],
        aggregate_base_rate=0.0,
        disagreement=result.disagreement,
    )
    anchor, _rule = checks.anchor_from(view, decomposition)
    view.aggregate_base_rate = min(1.0, max(0.0, anchor if anchor is not None else 0.0))
    return view


def merge_base_rates(
    sub_questions: list[SubPrediction],
    results: list[SubQuestionBaseRates],
    decomposition: Decomposition,
) -> OutsideView:
    """Fold every researched lens into one OutsideView, stamped with its sub-question.

    The stamp is unconditional rather than "only if the model left it empty". A cell
    measured exactly one population for exactly one sub-question; letting it volunteer a
    different id would re-open the linkage hole `check_linkage` closes.

    The weight and population definition are taken from the *chosen* lens rather than
    from whatever came back, so a research cell cannot quietly re-weight its own
    population after seeing what it measured — which is the entire reason choosing and
    measuring are separate steps.

    `aggregate_base_rate` is computed by `checks.anchor_from`, the same function
    `check_aggregation` re-derives it with.
    """
    merged: list[ResearchedLens] = []
    notes: list[str] = []

    for sub_question, result in zip(sub_questions, results):
        if not isinstance(result, SubQuestionBaseRates):
            continue
        merged.append(
            result.lens.model_copy(update={"sub_question_ids": [sub_question.id]})
        )
        if result.disagreement.strip():
            notes.append(f"{sub_question.id}: {result.disagreement.strip()}")

    view = OutsideView(
        lenses=merged,
        aggregate_base_rate=0.0,
        disagreement=" · ".join(notes),
    )
    anchor, _rule = checks.anchor_from(view, decomposition)
    view.aggregate_base_rate = min(1.0, max(0.0, anchor if anchor is not None else 0.0))
    return view


async def whole_question_outside(
    input: ForecastInput,
    decomposition: Decomposition | None,
    deps: ForecastDeps,
    errors: list[str],
) -> OutsideView:
    """No column produced a base rate. Fall back, or say why we cannot.

    Two ways to get here. Either the decomposition labelled nothing researchable — rare,
    `check_decomposition`'s P2 arm discourages it — or every column that ran failed. The
    first is a legitimate fallback; the second is a run that has nothing to stand on, and
    an invented anchor would be worse than an error.
    """
    real = [e for e in errors if e]
    if real and all("UsageLimitExceeded" in e for e in real):
        raise UsageLimitExceeded(
            f"every lens exhausted its search budget without returning a base rate "
            f"({'; '.join(real)}). Resume with a higher search depth."
        )
    if real:
        raise RuntimeError(f"every lens failed to measure: {'; '.join(real)}")

    assert decomposition is not None
    return await _whole_question_cell(input, decomposition, deps)
