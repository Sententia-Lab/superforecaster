"""Inside-view agents — principles 5 and 9.

Adjust away from the base rate using case-specific evidence. Every adjustment is a
signed delta from the outside view, never a fresh absolute estimate — that is what
makes principle 5 ("the inside view modifies the outside view") checkable arithmetic
rather than a hope.

One agent per column, like the base-rate row above it, and each is seeded with **its own
sub-question's** rate rather than the whole-question anchor. For a decomposed question
the global anchor is the wrong reference point: an adjustment about whether the docs get
filed in time is not a delta from the probability of the whole IPO.

The fan-out is a `.map()` edge in `graphs.forecast`; this module supplies one cell. P14
and P15 belong to `reflect`, its own step after this row's barrier — see that module for
why they cannot be asked of one column.
"""

from __future__ import annotations

from config import get_cell_limits, resolve_agent_model
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from .. import checks
from ..deps import ForecastDeps
from ..models import (
    Adjustment,
    ForecastInput,
    OutsideView,
    SubClaimAdjustments,
    SubPrediction,
)
from ..observability import run_agent
from ..tools import find_disconfirming_evidence, search_web, search_wikipedia
from . import as_of_note, attach_budget_pressure, format_question, with_model

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


async def whole_question_adjustments(
    input: ForecastInput,
    outside: OutsideView,
    deps: ForecastDeps,
    errors: list[str],
) -> tuple[list[Adjustment], dict[str, str]]:
    """No column produced an adjustment. Fall back, or say why we cannot.

    Two ways to get here. Either no column carried a reference class — so there was
    nothing to adjust *from*, which is principle 5's premise — or every column that ran
    failed. The first adjusts from the whole-question anchor; the second is a run with
    nothing to stand on, and inventing adjustments would be worse than an error.
    """
    real = [e for e in errors if e]
    if real and all("UsageLimitExceeded" in e for e in real):
        raise UsageLimitExceeded(
            f"every column exhausted its search budget without returning an adjustment "
            f"({'; '.join(real)}). Resume with a higher search depth."
        )
    if real:
        raise RuntimeError(f"every inside-view column failed: {'; '.join(real)}")

    fallback = SubPrediction(
        question=input.question,
        probability=outside.aggregate_base_rate,
        rationale="no sub-question carried its own reference class",
        knowability="judgment",
    ).model_copy(update={"id": None})

    result = await run_inside_view_cell(input, fallback, outside, deps)
    return result.adjustments, {"whole question": result.steel_man}
