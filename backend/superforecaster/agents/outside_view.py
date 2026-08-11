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

from ..config import get_budget, get_model_settings, resolve_agent_model
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

INSTRUCTIONS = """# ROLE
You are an expert researcher that finds and records the occurrences of historical events with a limited search budget. You measure ONE population of data and report what you counted. You do not forecast.

# TASK
You'll be given a question asking for the rate of occurrence of an event, and a population to measure it over. Your job is to answer that question with data that is PUBLISHED or COUNTED.

- PUBLISHED data is a statistic published from a credible source ("61% of public companies have a revenue above x").
- COUNTED data is raw data pulled from an index, table, survey or "list of" article. Take every relevant case from that page and record it.

## Search in this order:
  1. Find a published statistic for this population. One search — the cheapest full answer.
  2. If (1) fails, find a page listing many cases at once: an index, table, survey, or "list of" article.
     One or two searches. Take every case from that one page.
  3. If (2) fails, find individual cases from disparate sources and count them; one search each, at most three. Do not chase a tenth case.

# CONSTRAINTS
1. Every number comes from cases you found or a statistic someone else published. `hits` and `n` are counts. A rate you reasoned your way to is not a base rate.
2. Never repeat a search with reworded terms. A query that found nothing means this population is hard to measure, not that the words were wrong.
3. Stop at whichever comes first:
  - you hold a published block, or a counted block with 3 or more named cases;
  - two searches in a row return nothing new;
  - two searches remain.

## WHEN YOU CANNOT MEASURE THIS POPULATION
Say so. Return whatever you found, graded `low`, and name the problem in `disagreement`.
That is an honest answer, not a failure — a later step can use a thin rate it knows is
thin, and cannot use a confident one that is wrong.

Do not measure a different population instead. The boundary you were given was chosen
before anyone looked anything up, on purpose, and a later step re-imposes it on whatever
you return — so a wider class you measure quietly becomes a rate reported under the
narrow class's name.

## GRADE EACH SOURCE FOR THIS RATE, NOT FOR ITS REPUTATION
  high    a dataset or study measuring this population directly
  medium  relevant but indirect — adjacent population, older data, partial coverage
  low     a single report, a secondhand figure, or a number you had to infer

Say in `note` what makes it that grade.

A counted block must name every case in `analogs`, because a check compares that list against `n` and `hits`. Three cases you can name beat ten you cannot.

Your searches run out. When they do the search tools are withdrawn, and whatever you hold
at that moment becomes the answer — so land deliberately rather than being cut off.
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

If nothing measures it, say so in `disagreement` and grade what you did find `low`. Do not
measure a nearer population instead.

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
    # The same six fields `stages.run_base_rate_step` re-imposes on the main path. Without
    # this the fallback keeps whatever population and weight the model returned, so the one
    # path with no oversight is also the one where a cell may name its own reference class
    # after measuring it — which is what choosing lenses blind exists to prevent (ADR 40).
    measured = result.lens.model_copy(
        update={
            "name": whole.name,
            "population": whole.population,
            "why_it_fits": whole.why_it_fits,
            "weight": whole.weight,
            "weight_rationale": whole.weight_rationale,
            "sub_question_ids": [],
        }
    )
    view = OutsideView(
        lenses=[measured],
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

    `sub_question_ids` is the only field this touches. The identity and weight were already
    re-imposed from the *chosen* lens by `stages.run_base_rate_step`, which is where a
    research cell is stopped from re-weighting its own population after seeing what it
    measured. This docstring used to claim that job, and did not do it — the results
    arriving here have been through it already.

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
