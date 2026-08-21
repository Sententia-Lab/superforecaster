"""Base-rate agent — principle 4. Measures ONE population that `lenses` named.
Rates are counted, never stated: the agent returns evidence blocks and
`checks.lens_rate` divides."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks

from ..config import get_budget
from ..deps import ForecastDeps
from ..models import BaseRateResult, ForecastInput, Lens, SubPrediction
from ..runner import run_agent
from ..tools import extract_pages, search_research, search_web, search_wikipedia
from . import GRADING, format_question, withdraw_tools

INSTRUCTIONS = f"""You measure ONE population and report what you counted. You do not
forecast.

Answer with data that is PUBLISHED or COUNTED:
- PUBLISHED: a statistic from a credible source ("61% of public companies ...").
- COUNTED: cases you took from an index, table, survey, or "list of" article.

Search in this order:
  1. A published statistic for this population. One search.
  2. A page listing many cases at once. One or two searches; take every case from it.
  3. Individual cases from separate sources, at most three searches.

Rules:
- `hits` and `n` are counts of cases you found or a statistic someone published. A rate
  you reasoned your way to is not a base rate.
- Never repeat a search with reworded terms.
- Stop when you hold a published block or a counted block with 3+ named cases, or when
  two searches in a row return nothing new.
- A counted block must name every case in `analogs`; a check compares that list to
  `n` and `hits`.
- If nothing measures this population, return what you found graded `low` and say so
  in `disagreement`. Do not measure a different population.

{GRADING}"""

agent = Agent[ForecastDeps, BaseRateResult](
    name="base_rate_cell",
    deps_type=ForecastDeps,
    output_type=BaseRateResult,
    instructions=INSTRUCTIONS,
    tools=[search_research, search_web, extract_pages, search_wikipedia],
    capabilities=[Hooks(prepare_tools=withdraw_tools)],
    retries=1,
)


async def run_research_lens(
    input: ForecastInput, sub_question: SubPrediction, lens: Lens, deps: ForecastDeps
) -> BaseRateResult:
    prompt = f"""Measure ONE population.

{format_question(input)}

THE PART THIS BEARS ON — {sub_question.id}: {sub_question.question}

YOUR POPULATION — {lens.name}
Who is in it: {lens.population}
Why it was chosen: {lens.why_it_fits}

Find published or counted data for this population and nothing else."""
    result = await run_agent(
        agent,
        prompt,
        deps=deps,
        budget=get_budget("base_rate_cell", max_iterations=input.max_iterations),
        run_name=f"base rates · {sub_question.id} · {lens.name}",
    )
    return result.output
