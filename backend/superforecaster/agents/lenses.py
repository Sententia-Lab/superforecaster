"""Lens-choosing agent — principles 4 and 7.

Name the reference populations for one sub-question, **before any rate has been seen**.

That ordering is the whole point of this being its own step. A single agent that chose
populations and measured them in one pass could quietly settle on whichever population
gave the answer it already liked, and nothing downstream could tell — the output would
look identical either way. Naming them blind is pre-registration: the populations are
committed to first, and whatever they turn out to say is what the forecast has to live
with.

No tools. Choosing a reference class is an act of judgment about what this case resembles,
and a search at this stage would only surface rates, which is exactly what this step must
not see.
"""

from __future__ import annotations

from config import resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import Decomposition, ForecastInput, SubClaimLenses, SubPrediction
from ..observability import run_agent
from . import as_of_note, format_question, with_model

INSTRUCTIONS = """You choose reference populations for ONE part of a forecasting question.
You do not look anything up and you do not estimate any probability. Another step
measures the populations you name; a later one adjusts them.

WHAT A LENS IS (principle 4)
A lens is a population of past cases your sub-question belongs to. "Will this startup be
acquired within 12 months" could be viewed through: all startups at this stage, all
startups in this sector, all companies this acquirer has approached before.

NAME ONE TO THREE (principle 7 — dragonfly eye)
One lens is a single viewpoint and will mislead you. Two or three that frame the
sub-question *differently* — broad and narrow, by-actor and by-sector, recent and
long-run — will disagree, and that disagreement is the most honest signal of uncertainty
you can produce. Prefer lenses that could disagree over lenses that restate each other.

Use one only when the sub-question genuinely admits a single sensible population.

DEFINE THE POPULATION SO IT COULD BE COUNTED
`population` is the load-bearing field. Someone else has to be able to take your
definition and enumerate the same cases. Write the boundary, not the vibe:

  bad   "large tech companies"
  good  "US-listed software companies with over $10B revenue that filed an S-1
         between 2015 and 2025"

A population nobody could count is a population nobody can check.

WEIGH THEM BY FIT, AND ONLY BY FIT
`weight` is how much this population resembles the case in front of you, relative to the
other lenses you are naming. It is **not** sample size and must not anticipate it. A
well-chosen population measured over twelve cases outweighs a poorly-chosen one measured
over two hundred — how well something was measured is a different question from whether
it is the right thing to measure.

`weight_rationale` has to argue for the number. This is the only judgment in the entire
pipeline that nothing downstream can verify: every other number is computed from evidence
and re-derivable. Yours is the one a reader has to either accept or reject on the
strength of what you wrote, so write it accordingly.

WHAT YOU MUST NOT DO
Do not state or guess a base rate. Do not describe what you expect the population to
show. If you already believe you know the rate, that belief is exactly what naming the
population first is meant to keep out of the answer.
"""


def build_lenses_agent(model: str | None = None) -> Agent[ForecastDeps, SubClaimLenses]:
    """Names populations for one sub-question. No tools, by design — see the module docstring."""
    return Agent[ForecastDeps, SubClaimLenses](
        model=model or resolve_agent_model(),
        name="lenses_agent",
        deps_type=ForecastDeps,
        output_type=SubClaimLenses,
        system_prompt=INSTRUCTIONS,
        retries=1,
    )


_agent: Agent[ForecastDeps, SubClaimLenses] | None = None


def get_lenses_agent() -> Agent[ForecastDeps, SubClaimLenses]:
    global _agent
    if _agent is None:
        _agent = build_lenses_agent()
    return _agent


async def run_choose_lenses(
    input: ForecastInput,
    decomposition: Decomposition,
    sub_claim: SubPrediction,
    deps: ForecastDeps,
) -> SubClaimLenses:
    """Name 1-3 reference populations for one sub-question. No rates."""
    others = "\n".join(
        f"  - {s.id}: {s.question}"
        for s in decomposition.sub_claims
        if s.id != sub_claim.id
    )

    prompt = f"""Choose reference populations for ONE part of this question.

{format_question(input)}{as_of_note(deps)}

YOUR PART — {sub_claim.id}: {sub_claim.question}
Why the decomposition split it out: {sub_claim.rationale}

THE OTHER PARTS, being handled separately — do not choose lenses for these:
{others or "  (none)"}

Return a SubClaimLenses with 1-3 lenses for your part. Define each population precisely
enough that someone else could count the same cases, and weigh them by fit alone."""

    agent = get_lenses_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            run_name=f"lenses · {sub_claim.id}",
        )
    return result.output
