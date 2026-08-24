"""Lens-choosing agent — principles 4 and 7. Names populations for one sub-question
before any rate is seen (pre-registration, ADR 40). No tools, so it cannot peek."""

from __future__ import annotations

from pydantic_ai import Agent

from ..config import get_budget
from ..deps import ForecastDeps
from ..models import Decomposition, ForecastInput, SubPrediction, SubQuestionLenses
from ..runner import run_agent
from . import format_question

INSTRUCTIONS = """You choose reference populations (lenses) for ONE part of a forecasting
question. You do not look anything up and you do not estimate a probability.

A lens is a population of past cases the sub-question belongs to. Name one to three
that frame it differently — broad and narrow, by actor and by sector, recent and
long-run — so that they can disagree (principle 7). Use one only when a single
population is the sensible choice.

DEFINE THE POPULATION SO IT COULD BE COUNTED
  bad   "large tech companies"
  good  "US-listed software companies with over $10B revenue that filed an S-1
         between 2015 and 2025"

THEN MAKE IT ONE SOMEBODY HAS ALREADY COUNTED
The next step has a handful of searches. Every extra condition cuts the chance a
published source measured this exact thing. Ask: would a dataset, a study, an index,
or a "list of" article plausibly cover it? If not, drop a condition.
  too narrow  midterms since 1946 where the out-party led the generic ballot by 3+
              points in the final 60 days
  countable   post-war midterm seat change for the president's party
Say what you gave up in `why_it_fits`.

WEIGH BY FIT ONLY
`weight` is how much this population resembles the case, relative to the other lenses.
Never sample size. `weight_rationale` argues for the number — it is the one judgment
nothing downstream can verify.

Do not state or guess a base rate."""

agent = Agent[ForecastDeps, SubQuestionLenses](
    name="lenses",
    deps_type=ForecastDeps,
    output_type=SubQuestionLenses,
    instructions=INSTRUCTIONS,
    retries=1,
)


async def run_choose_lenses(
    input: ForecastInput,
    decomposition: Decomposition,
    sub_question: SubPrediction,
    deps: ForecastDeps,
) -> SubQuestionLenses:
    others = "\n".join(
        f"  - {s.id}: {s.question}"
        for s in decomposition.sub_questions
        if s.id != sub_question.id
    )
    prompt = f"""Choose reference populations for ONE part of this question.

{format_question(input)}

YOUR PART — {sub_question.id}: {sub_question.question}
Why the decomposition split it out: {sub_question.rationale}

THE OTHER PARTS, handled separately — do not choose lenses for these:
{others or "  (none)"}"""
    result = await run_agent(
        agent,
        prompt,
        deps=deps,
        budget=get_budget("lenses"),
        run_name=f"lenses · {sub_question.id}",
    )
    return result.output
