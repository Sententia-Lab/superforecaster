"""Inside-view agent — principles 5, 9, and 14. Moves ONE lens's measured rate by
signed adjustments, so principle 5 is checkable arithmetic."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks

from .. import checks
from ..config import get_budget
from ..deps import ForecastDeps
from ..models import AdjustmentResult, ForecastInput, ResearchedLens, SubPrediction
from ..runner import run_agent
from ..tools import extract_pages, search_research, search_web, search_wikipedia
from . import GRADING, format_question, withdraw_tools

INSTRUCTIONS = f"""You supply the INSIDE VIEW for ONE part of a question: what makes this
case differ from its reference population. You do not produce a final probability.

ADJUST, DO NOT REPLACE (principle 5)
Every adjustment is a signed move from the rate you are given, in probability points
(`direction` up/down/neutral, `magnitude` 0 to 0.5). If the rate is 0.20 and this case
is somewhat more likely, that is one adjustment up by 0.08 — never "I think 0.28".
Size each move against this population only; a later step blends and combines.

ASK WHAT THIS POPULATION ALREADY ACCOUNTS FOR
  population "large-cap tech IPOs", evidence "this company is very large"
    -> no adjustment. Being large-cap is what the population IS.
  population "all AI labs, any size", evidence "this company is very large"
    -> a real adjustment.

THE FLIP TEST (principle 9)
For every adjustment, `flip_test` says what the opposite evidence would have done. If
the answer is "nothing much", set `is_noise=true` and `magnitude=0`. Keep it listed.

SEEK DISCONFIRMATION (principle 14)
Run at least one search for evidence against your conclusion. `steel_man` is the
strongest case against it for THIS part, argued as a believer would.

{GRADING}
Leave `sources` empty when an adjustment is a judgment call; that grades as weak
support, which is honest."""

agent = Agent[ForecastDeps, AdjustmentResult](
    name="inside_view",
    deps_type=ForecastDeps,
    output_type=AdjustmentResult,
    instructions=INSTRUCTIONS,
    tools=[search_research, search_web, extract_pages, search_wikipedia],
    capabilities=[Hooks(prepare_tools=withdraw_tools)],
    retries=1,
)


async def run_adjust_lens(
    input: ForecastInput,
    sub_question: SubPrediction,
    lens: ResearchedLens,
    disagreement: str,
    deps: ForecastDeps,
) -> AdjustmentResult:
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

{format_question(input)}

THE PART THIS BEARS ON — {sub_question.id}: {sub_question.question}

YOUR POPULATION — {lens.name}
Who is in it: {lens.population}
MEASURED RATE: {rate:.3f}

WHAT THAT RATE WAS COUNTED FROM:
{blocks}
{f"\nCASES BEHIND IT:\n{cases}" if cases else ""}
{f"\nWHAT THIS POPULATION ALREADY ACCOUNTS FOR:\n  {disagreement}" if disagreement.strip() else ""}

Every adjustment is a signed delta from {rate:.3f}, each with a flip test."""
    result = await run_agent(
        agent,
        prompt,
        deps=deps,
        budget=get_budget("inside_view", max_iterations=input.max_iterations),
        run_name=f"inside view · {sub_question.id} · {lens.name}",
    )
    return result.output
