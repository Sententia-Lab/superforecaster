"""Inside-view agents — principles 5 and 9.

Adjust away from the base rate using case-specific evidence. Every adjustment is a
signed delta from the outside view, never a fresh absolute estimate — that is what
makes principle 5 ("the inside view modifies the outside view") checkable arithmetic
rather than a hope.

One agent per **lens**, seeded with that lens's own base rate. A modifier is only
meaningful relative to a population: "the market cap exploded" is already inside
*large-cap tech IPOs* and warrants nothing, while against *all AI labs* it is the whole
differentiator. Moving a blended rate would double-count against the populations that
already control for the feature, and under-count against the ones that do not.

That is also why the cell is told what its population already accounts for — the research
step recorded it, and it is the single most useful thing to know before adjusting.

The fan-out is handled by `stages`; this module supplies one cell. P14
and P15 belong to `reflect`, its own step after the barrier — see that module for why
they cannot be asked of one lens.
"""

from __future__ import annotations

from ..config import get_budget, get_model_settings, resolve_agent_model
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from .. import checks
from ..deps import ForecastDeps
from ..models import (
    Adjustment,
    ForecastInput,
    OutsideView,
    ResearchedLens,
    SubQuestionAdjustments,
    SubPrediction,
)
from ..observability import run_agent
from ..tools import find_disconfirming_evidence, search_web, search_wikipedia
from . import (
    as_of_note,
    attach_budget,
    format_question,
    withdraw_spent_tools,
    with_model,
)

INSTRUCTIONS = """You supply the INSIDE VIEW for ONE part of a larger question: what
makes this specific case differ from its reference class. You do not produce a final
probability — a later step does.

The other parts of the question are being worked on separately, at the same time, by
other agents. Say nothing about them.

ADJUST, DO NOT REPLACE (principle 5)
You are given ONE population and the rate measured within it. Every adjustment you return
is a signed move away from **that** rate, in probability points:
    direction  up / down / neutral
    magnitude  how many points, 0 to 0.5
Never restate an absolute probability. If the rate is 0.20 and you think this case is
somewhat more likely, that is one adjustment up by 0.08 — not "I think 0.28".

Size every move against the population in front of you and nothing else. A later step
blends the adjusted populations and combines the sub-questions; you do not need to
anticipate any of that, and trying to will distort your number.

NAME EVERY ADJUSTMENT
Give each adjustment a `title` of six words or fewer. Name the mechanism, not the
direction — "already cutting subgroup has analogs", not "raises the estimate". A reader
scans the titles to find the move they want; `evidence` is where the argument lives.

ASK WHAT THIS POPULATION ALREADY ACCOUNTS FOR
This is the question that separates a real adjustment from double-counting. Your
population has a definition, and everything inside that definition is *already priced
into* the rate you were given.

  population "large-cap tech IPOs", evidence "this company is very large"
    -> no adjustment. Being large-cap is what the population IS.

  population "all AI labs, any size", evidence "this company is very large"
    -> a real adjustment. Size is what distinguishes this case from the population.

The same fact, opposite treatment, decided entirely by which population you are looking
through. Write what you deliberately did not adjust for in `already_controlled_for` — a
reader has no other way to tell a considered omission from an oversight.

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
) -> Agent[ForecastDeps, SubQuestionAdjustments]:
    """One column's inside-view researcher."""
    agent = Agent[ForecastDeps, SubQuestionAdjustments](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
        name="inside_view",
        deps_type=ForecastDeps,
        output_type=SubQuestionAdjustments,
        system_prompt=INSTRUCTIONS,
        tools=[search_web, search_wikipedia, find_disconfirming_evidence],
        prepare_tools=withdraw_spent_tools,
        retries=1,
    )
    attach_budget(agent)
    return agent


_agent: Agent[ForecastDeps, SubQuestionAdjustments] | None = None


def get_inside_view_agent() -> Agent[ForecastDeps, SubQuestionAdjustments]:
    global _agent
    if _agent is None:
        _agent = build_inside_view_agent()
    return _agent


async def run_adjust_lens(
    input: ForecastInput,
    sub_question: SubPrediction,
    lens: ResearchedLens,
    already_controlled_for: str,
    deps: ForecastDeps,
) -> SubQuestionAdjustments:
    """Adjust ONE population's measured rate. Searches; budget-limited."""
    rate = checks.lens_rate(lens)
    blocks = "\n".join(
        f"  - {e.kind}: {e.hits} of {e.n} — {e.note}"
        + (f" [{e.source.source}]" if e.source else "")
        for e in lens.evidence
    )
    cases = "\n".join(
        f"  - {'YES' if a.outcome >= 1.0 else 'no '}  {a.description}"
        for a in lens.analogs[:12]
    )

    prompt = f"""Adjust the measured rate for ONE population.

{format_question(input)}{as_of_note(deps)}

THE PART OF THE QUESTION THIS BEARS ON — {sub_question.id}: {sub_question.question}

YOUR POPULATION — {lens.name}
Who is in it: {lens.population}
MEASURED RATE: {rate:.3f}

WHAT THAT RATE WAS COUNTED FROM:
{blocks}
{f"\nCASES BEHIND IT:\n{cases}" if cases else ""}
{f"\nWHAT THIS POPULATION ALREADY ACCOUNTS FOR:\n  {already_controlled_for}" if already_controlled_for.strip() else ""}

Return a SubQuestionAdjustments. Every adjustment is a signed delta from {rate:.3f} — the
rate for THIS population, not for the question — each with a flip test. Say in
`already_controlled_for` what you deliberately did not adjust for and why."""

    agent = get_inside_view_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            budget=get_budget(agent.name, max_iterations=input.max_iterations),
            run_name=f"inside view · {sub_question.id} · {lens.name}",
        )
    return result.output


async def whole_question_adjustments(
    input: ForecastInput,
    outside: OutsideView,
    deps: ForecastDeps,
    errors: list[str],
) -> tuple[list[Adjustment], dict[str, str]]:
    """No lens produced an adjustment. Fall back, or say why we cannot.

    Two ways to get here. Either no lens was researched at all — so there was nothing to
    adjust *from*, which is principle 5's premise — or every cell that ran failed. The
    first adjusts the first available lens; the second is a run with nothing to stand on,
    and inventing adjustments would be worse than an error.
    """
    real = [e for e in errors if e]
    if real and all("UsageLimitExceeded" in e for e in real):
        raise UsageLimitExceeded(
            f"every lens exhausted its search budget without returning an adjustment "
            f"({'; '.join(real)}). Resume with a higher search depth."
        )
    if real:
        raise RuntimeError(f"every inside-view cell failed: {'; '.join(real)}")

    if not outside.lenses:
        raise RuntimeError("no lens to adjust from")

    lens = outside.lenses[0]
    fallback = SubPrediction(
        question=input.question,
        probability=checks.lens_rate(lens),
        rationale="whole-question fallback",
        knowability="judgment",
    ).model_copy(update={"id": None})

    result = await run_adjust_lens(input, fallback, lens, "", deps)
    # Named after the lens so `adjusted_lens_rate` picks them up like any other.
    adjustments = [
        a.model_copy(
            update={"lens_name": lens.name, "sub_question_ids": lens.sub_question_ids}
        )
        for a in result.adjustments
    ]
    return adjustments, {"whole question": result.steel_man}
