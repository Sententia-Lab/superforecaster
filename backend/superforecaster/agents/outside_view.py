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

from config import get_model_settings, get_cell_budget, get_cell_limits, resolve_agent_model
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from .. import checks
from ..deps import ForecastDeps, SearchBudget
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
from . import as_of_note, attach_budget_pressure, format_question, with_model

INSTRUCTIONS = """You measure ONE population. It has already been chosen and defined for
you, and its definition is not yours to revise. You do not produce a probability for the
question, you do not reason about what makes this case special, and you do not judge how
well the population fits — other steps do all three. Your only job is: **within this
population, how often did the thing happen?**

COUNT, DO NOT ESTIMATE (principle 4)
You return evidence blocks. The rate is divided out of them by code, so there is no field
in which to state one, and no way for your number and your cases to disagree.

Two kinds of block, and you may return several of either:

  counted    Cases you actually found and can name. Set `n` to how many you looked at
             and `hits` to how many of those did the thing. List every one of them in
             `analogs`, with `outcome` 1.0 for did and 0.0 for did not. A check verifies
             `n` against how many analogs you listed and `hits` against how many resolved
             yes, so a count with no cases behind it fails.

  published  A statistic someone else measured — "61% of 230 S-1 filings priced within
             the year". Set `hits` and `n` from the statistic and cite the `source`.
             This is how a population of 230 gets into a forecast that nobody could
             enumerate by hand. Every published block needs a source; a statistic with
             no provenance is an assertion.

Blocks pool into one rate: 7 counted out of 10 plus 140 published out of 230 is 147/240.
So a handful of cases you verified yourself and a large published study can sit in the
same base rate, each carrying exactly the weight of its own denominator.

SEARCH FOR IT
A rate you reasoned your way to is not a base rate. If the population turns out to be
hard to measure, say so in `disagreement` and return the thin evidence you have —
honestly small is worth more than confidently invented.

SAY WHAT THIS POPULATION ALREADY ACCOUNTS FOR
`disagreement` is where you write what this population might mislead about, and — this
part matters downstream — what it already controls for. If your population is "large-cap
tech IPOs", then being large-cap is *already priced in*, and a later step that adjusts
upward for the company being large would be counting it twice. You are the only step that
knows this, so say it.

GRADE YOUR SOURCES
Every class needs at least one entry in `sources`. Grade each for how strongly it
supports *this specific* base rate, not how reputable it is in general:
    high    a real dataset or study measuring this population directly
    medium  relevant but indirect — adjacent population, older data, partial coverage
    low     a single report, a secondhand figure, or a number you had to infer
Say why in `note`. Set `source` to a human label — the publication, dataset, or filing
("PitchBook M&A Report 2024"), never a bare URL. Put the link in `url`, and only when
you actually retrieved that page: a check verifies cited URLs against what your searches
returned, and inventing one is a violation. Copy the link exactly as the search results
gave it, in full — a partial or redirect fragment is dropped and the citation loses its
link.

Padding the list with weak citations does not help you — a class is graded by its
*strongest* source, so an extra thin one neither raises nor lowers it. If the evidence
is genuinely thin, say so in the evidence `note` and return fewer counted rows rather
than inventing a rate.
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
        retries=1,
    )
    attach_budget_pressure(agent)
    return agent


_cell_agent: Agent[ForecastDeps, SubQuestionBaseRates] | None = None


def get_base_rate_cell_agent() -> Agent[ForecastDeps, SubQuestionBaseRates]:
    global _cell_agent
    if _cell_agent is None:
        _cell_agent = build_base_rate_cell_agent()
    return _cell_agent


def cell_deps(deps: ForecastDeps, sub_question_id: str, max_iterations: int) -> ForecastDeps:
    """A deps copy bound to one column, with its own budget and its own source list.

    The private `sources_seen` is not a style choice: `observability` detects new sources
    by remembering how long that list was and slicing off the tail, and two cells
    appending to one list makes that index hand each cell the other's sources. The parent
    extends from these after the barrier.
    """
    soft, hard = get_cell_budget(max_iterations)
    return replace(
        deps,
        budget=SearchBudget(sub_question=sub_question_id, soft_depth=soft, hard_depth=hard),
        sources_seen=[],
    )


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

Count within this population and nothing else. Do not redefine it, do not substitute a
population you find easier to search, and do not weigh it against any other — that has
already been decided.

Return a SubQuestionBaseRates whose `lens` repeats the population exactly as given and adds
your evidence blocks and analogs."""

    agent = get_base_rate_cell_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            usage_limits=get_cell_limits(input.max_iterations),
            run_name=f"base rates · {sub_question.id} · {lens.name}",
        )
    return result.output


def exhausted_notice(deps: ForecastDeps) -> None:
    """Mark a cell as having blown its hard cap, and say so on the wire.

    `UsageLimitExceeded` is raised *before* the tools run, so by the time it reaches a
    caller there is no output to salvage — the cell contributes nothing and the column
    falls back to its own working estimate in `checks.chain_inputs`. The run continues.

    That fallback is why one greedy column no longer costs the other three their work,
    which is the whole point of moving the budget from the row to the cell.
    """
    b = deps.budget
    if b is None:
        return
    b.exhausted = True
    if deps.emit is not None:
        deps.emit(
            "exhausted",
            {
                "id": b.sub_question,
                "used": b.used,
                "soft_depth": b.soft_depth,
                "hard_depth": b.hard_depth,
                "recovered": False,
            },
            b.sub_question,
        )


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
        merged.append(result.lens.model_copy(update={"sub_question_ids": [sub_question.id]}))
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
