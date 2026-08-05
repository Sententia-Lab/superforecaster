"""Inside-view agents — principles 5 and 9.

Adjust away from the base rate using case-specific evidence. Every adjustment is a
signed delta from the outside view, never a fresh absolute estimate — that is what
makes principle 5 ("the inside view modifies the outside view") checkable arithmetic
rather than a hope.

One agent per column, like the base-rate row above it, and each is seeded with **its own
sub-question's** rate rather than the whole-question anchor. For a decomposed question
the global anchor is the wrong reference point: an adjustment about whether the docs get
filed in time is not a delta from the probability of the whole IPO.

P14 and P15 moved to `reflect`, which runs after this row's barrier — see that module for
why they cannot be asked of one column.
"""

from __future__ import annotations

import asyncio

from config import get_cell_limits, resolve_agent_model
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from .. import checks
from ..deps import ForecastDeps
from ..models import (
    Adjustment,
    Decomposition,
    ForecastInput,
    InsideView,
    OutsideView,
    SubClaimAdjustments,
    SubPrediction,
)
from ..observability import run_agent
from ..tools import find_disconfirming_evidence, search_web, search_wikipedia
from . import as_of_note, attach_budget_pressure, format_question, with_model
from .outside_view import cell_deps, exhausted_notice
from .reflect import run_reflect

INSTRUCTIONS = """You supply the INSIDE VIEW for ONE part of a larger question: what
makes this specific case differ from its reference class. You do not produce a final
probability — a later step does.

The other parts of the question are being worked on separately, at the same time, by
other agents. Say nothing about them.

ADJUST, DO NOT REPLACE (principle 5)
You are given the base rate for your sub-question. Every adjustment you return is a
signed move away from it, in probability points:
    direction  up / down / neutral
    magnitude  how many points, 0 to 0.5
Never restate an absolute probability. If the base rate is 0.20 and you think this case
is somewhat more likely, that is one adjustment up by 0.08 — not "I think 0.28".

Magnitudes are points on the FINAL probability, arrived at via this sub-question. A later
step adds every adjustment from every part of the question to the anchor, and a check
verifies the final number matches, so an adjustment sized against your own sub-question
in isolation will overstate its effect.

THE FLIP TEST (principle 9)
For every adjustment, fill in `flip_test`: what would my estimate do if I had found
the OPPOSITE of this evidence? If the honest answer is "nothing much", the evidence is
noise dressed as signal — set `is_noise=true` and `magnitude=0`. Keep it in the list;
recording what you considered and discarded is useful. Evidence you call noise must
move the number by zero.

SEEK DISCONFIRMATION FIRST (principle 14)
Use find_disconfirming_evidence before you settle, not after. Ordinary search returns
material that agrees with however you phrased the query.
    steel_man: the strongest version of the case against your conclusion for THIS
      sub-question — argue it properly, as someone who believes it would, not as a
      strawman you can dismiss.
    what_would_change_my_mind: the specific observation about this sub-question that
      would move you most.
A later step reads every part's counter-arguments together and writes the case against
the forecast as a whole, so keep yours scoped to your own part.

GRADE YOUR SOURCES
For each adjustment, list what backs it in `sources`. Grade each one for how strongly
it supports *this specific* adjustment, not how reputable it is in general:
    high    directly on point, and from something that would know
    medium  relevant but indirect, dated, or partial
    low     suggestive only — a single report, an analogy, a claim by an interested party
Say why in `note`. Set `source` to a human label — the publication, dataset, or filing
("PitchBook M&A Report 2024"), never a bare URL. Put the link in `url`, and only when
you actually retrieved that page: a check verifies cited URLs against what your searches
returned, and inventing one is a violation. Copy the link exactly as the search results
gave it, in full — a partial or redirect fragment is dropped and the citation loses its
link.

Leave `sources` empty when an adjustment is a judgment call with nothing to look up.
That is an honest answer and grades as weak support. Padding the list with weak
citations does not help you — the grade for a claim is its *strongest* source, so an
extra thin one neither raises nor lowers it.
"""


def build_inside_view_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, SubClaimAdjustments]:
    """One column's inside-view researcher."""
    agent = Agent[ForecastDeps, SubClaimAdjustments](
        model=model or resolve_agent_model(),
        name="inside_view_agent",
        deps_type=ForecastDeps,
        output_type=SubClaimAdjustments,
        system_prompt=INSTRUCTIONS,
        tools=[search_web, search_wikipedia, find_disconfirming_evidence],
        retries=1,
    )
    attach_budget_pressure(agent)
    return agent


_agent: Agent[ForecastDeps, SubClaimAdjustments] | None = None


def get_inside_view_agent() -> Agent[ForecastDeps, SubClaimAdjustments]:
    global _agent
    if _agent is None:
        _agent = build_inside_view_agent()
    return _agent


async def run_inside_view_cell(
    input: ForecastInput,
    sub_claim: SubPrediction,
    outside: OutsideView,
    deps: ForecastDeps,
) -> SubClaimAdjustments:
    """Adjust from ONE column's base rate. Searches; budget-limited."""
    own = checks.sub_claim_rate(sub_claim.id or "", outside)
    # None only on the whole-question fallback, where there is no per-column rate and the
    # anchor is the only base rate there is.
    rate = own if own is not None else outside.aggregate_base_rate
    relevant = checks.classes_for(sub_claim.id or "", outside) or (
        outside.reference_classes if own is None else []
    )
    classes = "\n".join(
        f"  - {rc.name}: {rc.base_rate:.3f} (n={rc.sample_size}, weight={rc.weight:.2f}, "
        f"{'; '.join(f'{s.source} [{s.confidence}]' for s in rc.sources)})"
        for rc in relevant
    )

    prompt = f"""Adjust from the base rate for ONE part of this question.

{format_question(input)}{as_of_note(deps)}

YOUR PART — {sub_claim.id}: {sub_claim.question}
Why the decomposition split it out: {sub_claim.rationale}

BASE RATE FOR THIS SUB-QUESTION: {rate:.3f}

THE REFERENCE CLASSES IT WAS BLENDED FROM:
{classes}

Return a SubClaimAdjustments. Every adjustment is a signed delta from {rate:.3f}, with a
flip test."""

    agent = get_inside_view_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            usage_limits=get_cell_limits(input.max_iterations),
            run_name=f"inside view · {sub_claim.id}",
        )
    return result.output


async def run_inside_view(
    input: ForecastInput,
    decomposition: Decomposition,
    outside: OutsideView,
    deps: ForecastDeps,
) -> InsideView:
    """One concurrent cell per column, then a reflect pass over all of them.

    Cells run for columns that have at least one reference class. A column with nothing
    researched has no base rate to adjust *from*, which is P5's whole premise — running a
    cell on it would be asking for an absolute estimate wearing a delta's clothes.
    """
    cells = [
        s
        for s in decomposition.sub_claims
        if s.id and checks.classes_for(s.id, outside)
    ]
    if not cells:
        return await _whole_question_cell(input, decomposition, outside, deps)

    cell_depses = [cell_deps(deps, s.id or "", input.max_iterations) for s in cells]

    async def cell(s: SubPrediction, d: ForecastDeps) -> SubClaimAdjustments | None:
        try:
            return await run_inside_view_cell(input, s, outside, d)
        except UsageLimitExceeded:
            # This column searched past its wall. Degrade it, keep the row.
            exhausted_notice(d)
            return None

    results = await asyncio.gather(
        *(cell(s, d) for s, d in zip(cells, cell_depses)),
        return_exceptions=True,
    )
    for d in cell_depses:
        deps.sources_seen.extend(d.sources_seen)

    merged: list[Adjustment] = []
    steel_mans: dict[str, str] = {}
    for sub_claim, result in zip(cells, results):
        if not isinstance(result, SubClaimAdjustments):
            continue
        # Stamped by code for the same reason the reference classes are: a cell worked on
        # exactly one column, and a link it volunteered could point anywhere.
        merged.extend(
            a.model_copy(update={"sub_claim_ids": [sub_claim.id]})
            for a in result.adjustments
        )
        steel_mans[sub_claim.id or ""] = result.steel_man

    if not merged:
        # Nothing to reflect on and nothing for synthesis to add to the anchor. Raising
        # hands this to ADR 28's resume, which is the right response to "every column ran
        # out of searches" as well as to "the network was down".
        first = next((r for r in results if isinstance(r, BaseException)), None)
        if first is not None:
            raise first
        raise UsageLimitExceeded(
            "every column exhausted its search budget without returning an adjustment. "
            "Resume with a higher search depth."
        )

    reflection = await run_reflect(
        input, decomposition, outside, merged, steel_mans, deps
    )
    return InsideView(
        adjustments=merged,
        steel_man=reflection.steel_man,
        what_would_change_my_mind=reflection.what_would_change_my_mind,
        bias_checks=reflection.bias_checks,
    )


async def _whole_question_cell(
    input: ForecastInput,
    decomposition: Decomposition,
    outside: OutsideView,
    deps: ForecastDeps,
) -> InsideView:
    """No column carries a reference class, so adjust from the whole-question anchor.

    Reachable when the base-rate row fell back to its own whole-question path, which
    `check_decomposition`'s P2 arm already makes hard to reach. It exists so an outside
    view with no per-column attribution still produces something rather than crashing.
    """
    fallback = SubPrediction(
        question=input.question,
        probability=outside.aggregate_base_rate,
        rationale="no sub-question carried its own reference class",
        knowability="judgment",
    ).model_copy(update={"id": None})

    result = await run_inside_view_cell(input, fallback, outside, deps)
    reflection = await run_reflect(
        input,
        decomposition,
        outside,
        result.adjustments,
        {"whole question": result.steel_man},
        deps,
    )
    return InsideView(
        adjustments=result.adjustments,
        steel_man=reflection.steel_man,
        what_would_change_my_mind=reflection.what_would_change_my_mind,
        bias_checks=reflection.bias_checks,
    )
