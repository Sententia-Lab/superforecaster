"""Outside-view agents — principles 4 and 7.

Find reference classes and their base rates BEFORE any case-specific detail is
considered. The graph enforces the ordering; these agents supply the anchor.

One agent per column: decompose fixes a grid of sub-questions, and this row runs a cell
for each researchable one concurrently, then merges. `run_outside_view` keeps its
signature — the fan-out is this row's internal shape, not a graph edge, which is what
keeps `forecast_graph.get_nodes()` and every monkeypatch of this function intact.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from config import (
    get_cell_budget,
    get_cell_limits,
    get_research_limits,
    resolve_agent_model,
)
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from .. import checks
from ..deps import ForecastDeps, SearchBudget
from ..models import (
    Decomposition,
    ForecastInput,
    OutsideView,
    ReferenceClass,
    SubClaimBaseRates,
    SubPrediction,
)
from ..observability import run_agent
from ..tools import search_web, search_wikipedia
from . import as_of_note, attach_budget_pressure, format_question, with_model

INSTRUCTIONS = """You establish the OUTSIDE VIEW for ONE part of a larger question. You
do not produce a final probability, and you do not reason about what makes this case
special — a later step does that. Your only job is: how often does this kind of thing
happen?

The other parts of the question are being researched separately, at the same time, by
other agents. Say nothing about them. Spending your budget on the part of the question
you find easiest to search is the single most common way this step fails.

FIND REFERENCE CLASSES (principle 4)
A reference class is a population of past cases your sub-question belongs to. For "will
this startup be acquired within 12 months", candidates are: all startups at this stage,
all startups in this sector, all companies this acquirer has approached.

For each class, find the base rate — the fraction of that population where the thing
happened — and record how many cases it is drawn from and where the number came from.
Search for it. A rate you reasoned your way to is not a base rate.

USE AT LEAST TWO CLASSES (principle 7 — dragonfly eye)
One reference class is a single lens and will mislead you. Find at least two that frame
your sub-question differently. Broad and narrow, or by-actor and by-sector.

DISAGREEMENT IS INFORMATION, NOT AN INCONVENIENCE
If your classes give materially different rates — say 12% and 55% — that gap tells you
how uncertain this sub-question really is. Write it in `disagreement`: which class you
trust more here and why, and what the spread implies. Do NOT quietly average them and
move on. Leave `disagreement` empty only when the classes broadly agree.

WEIGH THEM
Set `weight` on each class: how well it fits THIS sub-question, relative to the others.
This is fit, not size — a class drawn from 10,000 cases that only glances at the
sub-question deserves a low weight. The weights are what blend your classes into the
rate for this part of the question, so a weight you did not think about is a rate you
did not think about.

You do not state that blended rate yourself. It is computed from your weights, which is
what stops the number and the classes behind it from telling different stories.

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
is genuinely thin, say so and lower `sample_size` rather than inventing a rate.
"""

WHOLE_QUESTION_INSTRUCTIONS = """You establish the OUTSIDE VIEW for a whole question. You
do not produce a final probability, and you do not reason about what makes this case
special — a later step does that. Your only job is: how often does this kind of thing
happen?

FIND REFERENCE CLASSES (principle 4)
A reference class is a population of past cases this question belongs to. Find the base
rate for each — the fraction of that population where the thing happened — and record
how many cases it is drawn from and where the number came from. Search for it. A rate
you reasoned your way to is not a base rate.

USE AT LEAST TWO CLASSES (principle 7 — dragonfly eye)
One reference class is a single lens and will mislead you. Find at least two that frame
the question differently.

DISAGREEMENT IS INFORMATION
If your classes give materially different rates, write in `disagreement` which you trust
more and what the spread implies. Do NOT quietly average them and move on.

AGGREGATE
Set `weight` on each class: how well it fits THIS question, relative to the others —
fit, not size. Then set `aggregate_base_rate` to the weighted average those weights
imply. A check recomputes it, so the two have to agree.

GRADE YOUR SOURCES
Every class needs at least one entry in `sources`, graded for how strongly it supports
*this specific* base rate:
    high    a real dataset or study measuring this population directly
    medium  relevant but indirect — adjacent population, older data, partial coverage
    low     a single report, a secondhand figure, or a number you had to infer
Set `source` to a human label, never a bare URL, and put the link in `url` only when you
actually retrieved that page — a check verifies cited URLs against what your searches
returned.
"""


def build_base_rate_cell_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, SubClaimBaseRates]:
    """One column's base-rate researcher."""
    agent = Agent[ForecastDeps, SubClaimBaseRates](
        model=model or resolve_agent_model(),
        name="base_rate_cell_agent",
        deps_type=ForecastDeps,
        output_type=SubClaimBaseRates,
        system_prompt=INSTRUCTIONS,
        tools=[search_web, search_wikipedia],
        retries=1,
    )
    attach_budget_pressure(agent)
    return agent


def build_outside_view_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, OutsideView]:
    """The whole-question fallback, for a decomposition with nothing researchable."""
    return Agent[ForecastDeps, OutsideView](
        model=model or resolve_agent_model(),
        name="outside_view_agent",
        deps_type=ForecastDeps,
        output_type=OutsideView,
        system_prompt=WHOLE_QUESTION_INSTRUCTIONS,
        tools=[search_web, search_wikipedia],
        retries=1,
    )


_cell_agent: Agent[ForecastDeps, SubClaimBaseRates] | None = None
_agent: Agent[ForecastDeps, OutsideView] | None = None


def get_base_rate_cell_agent() -> Agent[ForecastDeps, SubClaimBaseRates]:
    global _cell_agent
    if _cell_agent is None:
        _cell_agent = build_base_rate_cell_agent()
    return _cell_agent


def get_outside_view_agent() -> Agent[ForecastDeps, OutsideView]:
    global _agent
    if _agent is None:
        _agent = build_outside_view_agent()
    return _agent


def cell_deps(deps: ForecastDeps, sub_claim_id: str, max_iterations: int) -> ForecastDeps:
    """A deps copy bound to one column, with its own budget and its own source list.

    The private `sources_seen` is not a style choice: `observability` detects new sources
    by remembering how long that list was and slicing off the tail, and two cells
    appending to one list makes that index hand each cell the other's sources. The parent
    extends from these after the barrier.
    """
    soft, hard = get_cell_budget(max_iterations)
    return replace(
        deps,
        budget=SearchBudget(sub_claim=sub_claim_id, soft_depth=soft, hard_depth=hard),
        sources_seen=[],
    )


async def run_base_rate_cell(
    input: ForecastInput,
    decomposition: Decomposition,
    sub_claim: SubPrediction,
    deps: ForecastDeps,
) -> SubClaimBaseRates:
    """Research base rates for exactly one column. Searches; budget-limited."""
    others = "\n".join(
        f"  - {s.id}: {s.question}"
        for s in decomposition.sub_claims
        if s.id != sub_claim.id
    )

    prompt = f"""Establish the outside view for ONE part of this question.

{format_question(input)}{as_of_note(deps)}

YOUR PART — {sub_claim.id}: {sub_claim.question}
Why the decomposition split it out: {sub_claim.rationale}

The other parts, being researched separately right now by other agents:
{others or "  (none)"}

Research only your part. Return a SubClaimBaseRates with at least two reference
classes for it."""

    agent = get_base_rate_cell_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            usage_limits=get_cell_limits(input.max_iterations),
            run_name=f"base rates · {sub_claim.id}",
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
                "id": b.sub_claim,
                "used": b.used,
                "soft_depth": b.soft_depth,
                "hard_depth": b.hard_depth,
                "recovered": False,
            },
            b.sub_claim,
        )


async def _whole_question_cell(
    input: ForecastInput, decomposition: Decomposition, deps: ForecastDeps
) -> OutsideView:
    """The fallback when the decomposition labelled nothing researchable.

    `check_decomposition`'s P2 arm already blocks an all-judgment decomposition, so this
    is reachable only from a hand-built fixture — but it must not crash, and it is the
    one remaining producer of a reference class that names no column.
    """
    prompt = f"""Establish the outside view for this question.

{format_question(input)}{as_of_note(deps)}

DECOMPOSITION FROM THE PREVIOUS STEP:
{decomposition.model_dump_json(indent=2)}

No sub-claim was labelled researchable, so find the best reference class you can for the
question as a whole, and be explicit about how loose the fit is.

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


def _merge_base_rates(
    cells: list[SubPrediction],
    results: list[SubClaimBaseRates | BaseException | None],
    decomposition: Decomposition,
) -> OutsideView:
    """Fold every cell's classes into one OutsideView, stamped with their columns.

    The stamp is unconditional rather than "only if the model left it empty". A cell
    researched exactly one sub-question; letting it volunteer a different id would
    re-open the linkage hole `check_linkage` closes, and the group of classes belonging
    to no column at all — which the old flat prompt produced routinely — becomes
    impossible rather than merely unlikely.

    `aggregate_base_rate` is computed by `checks.anchor_from`, the same function
    `check_aggregation` re-derives it with.
    """
    merged: list[ReferenceClass] = []
    notes: list[str] = []

    for sub_claim, result in zip(cells, results):
        if not isinstance(result, SubClaimBaseRates):
            continue
        for rc in result.reference_classes:
            merged.append(rc.model_copy(update={"sub_claim_ids": [sub_claim.id]}))
        if result.disagreement.strip():
            notes.append(f"{sub_claim.id}: {result.disagreement.strip()}")

    view = OutsideView(
        reference_classes=merged,
        aggregate_base_rate=0.0,
        disagreement=" · ".join(notes),
    )
    anchor, _rule = checks.anchor_from(view, decomposition)
    view.aggregate_base_rate = min(1.0, max(0.0, anchor if anchor is not None else 0.0))
    return view


async def run_outside_view(
    input: ForecastInput,
    decomposition: Decomposition,
    deps: ForecastDeps,
) -> OutsideView:
    """Find reference classes and base rates, one concurrent cell per column.

    Cells run for the `researchable` columns only. A `judgment` column has, by its own
    label, no base rate to look up; a cell on it would either return nothing — violating
    `SubClaimBaseRates.min_length` — or invent one, which is exactly what
    `check_decomposition`'s P2 arm exists to discourage. It still gets a card, and it
    still contributes its own working estimate to the chain via `checks.chain_inputs`.
    """
    cells = [s for s in decomposition.sub_claims if s.knowability == "researchable"]
    if not cells:
        return await _whole_question_cell(input, decomposition, deps)

    cell_depses = [cell_deps(deps, s.id or "", input.max_iterations) for s in cells]

    async def cell(s: SubPrediction, d: ForecastDeps) -> SubClaimBaseRates | None:
        try:
            return await run_base_rate_cell(input, decomposition, s, d)
        except UsageLimitExceeded:
            # This column searched past its wall. Degrade it, keep the row.
            exhausted_notice(d)
            return None

    results = await asyncio.gather(
        *(cell(s, d) for s, d in zip(cells, cell_depses)),
        # One cell throwing for any *other* reason must not cancel its siblings
        # mid-search either. A failed column contributes no classes and falls back to
        # its own estimate in the chain.
        return_exceptions=True,
    )
    for d in cell_depses:
        deps.sources_seen.extend(d.sources_seen)

    if all(isinstance(r, BaseException) for r in results) and results:
        # Nothing was researched at all, and not because of the budget. Raising hands this
        # to ADR 28's checkpoint, which is the right place for "the network was down" —
        # unlike one column failing, or every column merely running out of searches.
        first = next(r for r in results if isinstance(r, BaseException))
        raise first

    if not any(isinstance(r, SubClaimBaseRates) for r in results):
        # Every column degraded, so there is no outside view to build — `OutsideView`
        # requires two classes and there are none. This is the one case a degraded cell
        # cannot absorb, and it is exactly what ADR 28's resume-with-a-higher-depth is
        # for, so say that rather than inventing an anchor from nothing.
        spent = ", ".join(
            f"{d.budget.sub_claim}={d.budget.used}"
            for d in cell_depses
            if d.budget is not None
        )
        raise UsageLimitExceeded(
            f"every column exhausted its search budget without returning a base rate "
            f"({spent}). Resume with a higher search depth."
        )

    return _merge_base_rates(cells, list(results), decomposition)
