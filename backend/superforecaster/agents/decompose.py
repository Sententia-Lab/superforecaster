"""Decompose agent — principles 1 and 2.

Fermi-ize the question into sub-claims, and label each one researchable or
judgment-required so effort goes where a base rate actually exists.
"""

from __future__ import annotations

from config import resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import Decomposition, ForecastInput
from ..observability import run_agent
from . import as_of_note, format_question, with_model

INSTRUCTIONS = """You break forecasting questions into tractable pieces. You do not
produce a final probability — a later step does that.

FERMI-IZE (principle 1)
Convert the question into 3-5 sub-claims that together determine the answer. Each
must be specific enough that someone could argue about it separately.

  "Will Company A acquire Company B by Q4?" becomes
    P(A is looking to acquire at all)
    x P(B is a plausible target | A is looking)
    x P(deal closes in the timeframe | interest exists)

Explain in chain_note how the sub-claims combine — multiply for a conjunction, take
the maximum for alternatives, and say which it is.

SEPARATE KNOWABLE FROM UNKNOWABLE (principle 2)
Label each sub-claim:
  researchable  a base rate or historical frequency could be looked up for this
  judgment      no lookup exists; this needs an estimate

Be honest about the split. Labelling everything "judgment" defeats the purpose — if
nothing is researchable, the outside view has nothing to anchor on. Labelling
something researchable that has no real reference class is worse: it sends the next
step hunting for a rate that does not exist.

Give each sub-claim a rough probability and a rationale. These are working estimates,
not the final answer — later steps will revise them against base rates and evidence.
"""


def build_decompose_agent(model: str | None = None) -> Agent[ForecastDeps, Decomposition]:
    return Agent[ForecastDeps, Decomposition](
        model=model or resolve_agent_model(),
        name="decompose_agent",
        deps_type=ForecastDeps,
        output_type=Decomposition,
        system_prompt=INSTRUCTIONS,
        retries=1,
    )


_agent: Agent[ForecastDeps, Decomposition] | None = None


def get_decompose_agent() -> Agent[ForecastDeps, Decomposition]:
    global _agent
    if _agent is None:
        _agent = build_decompose_agent()
    return _agent


async def run_decompose(input: ForecastInput, deps: ForecastDeps) -> Decomposition:
    """Break the question into labelled sub-claims. No tools — this is pure analysis."""
    prompt = f"""Decompose this forecasting question.

{format_question(input)}{as_of_note(deps)}

Return a Decomposition."""

    agent = get_decompose_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound, prompt, deps=deps, verbose=deps.verbose, run_name="decompose"
        )
    return result.output
