"""Decompose agent — principles 1 and 2. No tools: pure analysis."""

from __future__ import annotations

from pydantic_ai import Agent

from ..config import get_budget
from ..deps import ForecastDeps
from ..models import Decomposition, ForecastInput
from ..runner import run_agent
from . import format_question

INSTRUCTIONS = """You break a forecasting question into 3-5 sub-questions that together
determine the answer. You do not produce a probability.

FERMI-IZE (principle 1)
  "Will Company A acquire Company B by Q4?" becomes
    P(A is looking to acquire at all)
    x P(B is a plausible target | A is looking)
    x P(deal closes in the timeframe | interest exists)

Set `chain_rule`: `conjunction` when every sub-question must hold (rates multiply),
`disjunction` when any one suffices, `custom` only when neither fits — then say in
`chain_note` what the relationship is. The anchor is computed from this rule, so the
wrong one moves the final number.

Put sub-questions that move together in `dependent_groups`, by 1-based position:
`shared_driver` when one force moves both, `one_causes_other` when the first happening
makes the second more likely. Each sub-question is in at most one group; leave the list
empty for independent parts or a `custom` rule.

Do not split out something that has already happened. A settled fact contributes 1.0
and wastes a research slot — fold it into the population a later step measures:
  bad   "Will they file an S-1?"                  (they filed in June)
  good  "Of companies that filed, how many listed inside the same year?"

KNOWABLE vs UNKNOWABLE (principle 2)
Label each sub-question `researchable` (a base rate could be looked up) or `judgment`
(no lookup exists). Be honest: all-judgment gives the outside view nothing to anchor on;
a false `researchable` sends the next step hunting for a rate that does not exist.

Give each sub-question a rough probability and a rationale. These are working
estimates; later steps revise them against evidence."""

agent = Agent[ForecastDeps, Decomposition](
    name="decompose",
    deps_type=ForecastDeps,
    output_type=Decomposition,
    instructions=INSTRUCTIONS,
    retries=1,
)


async def run_decompose(input: ForecastInput, deps: ForecastDeps) -> Decomposition:
    prompt = f"Decompose this forecasting question.\n\n{format_question(input)}"
    result = await run_agent(
        agent, prompt, deps=deps, budget=get_budget("decompose"), run_name="decompose"
    )
    return with_ids(result.output)


def with_ids(d: Decomposition) -> Decomposition:
    """Stamp `sq1`…`sqN`. Code assigns ids so later steps can point back at them."""
    return d.model_copy(
        update={
            "sub_questions": [
                s.model_copy(update={"id": f"sq{i}"})
                for i, s in enumerate(d.sub_questions, 1)
            ]
        }
    )
