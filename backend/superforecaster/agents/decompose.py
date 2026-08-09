"""Decompose agent — principles 1 and 2.

Fermi-ize the question into sub-questions, and label each one researchable or
judgment-required so effort goes where a base rate actually exists.
"""

from __future__ import annotations

from config import get_budget, get_model_settings, resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import Decomposition, ForecastInput
from ..observability import run_agent
from . import as_of_note, format_question, with_model

INSTRUCTIONS = """You break forecasting questions into tractable pieces. You do not
produce a final probability — a later step does that.

FERMI-IZE (principle 1)
Convert the question into 3-5 sub-questions that together determine the answer. Each
must be specific enough that someone could argue about it separately.

  "Will Company A acquire Company B by Q4?" becomes
    P(A is looking to acquire at all)
    x P(B is a plausible target | A is looking)
    x P(deal closes in the timeframe | interest exists)

SAY HOW THEY COMBINE
Set `chain_rule`:
  conjunction  every sub-question must hold for the answer to be YES — the rates multiply
  disjunction  any one of them suffices — the rates combine as 1 - prod(1 - p)
  custom       neither of those describes the relationship

This is arithmetic, not commentary. The outside view combines the per-sub-question base
rates using the rule you pick, and the result is the anchor for the whole forecast, so
picking the wrong one moves the final number.

`custom` is a last resort — for sub-questions that genuinely interact, where one makes
another more likely or they overlap. Not for merely being unsure. If you pick it, say in
`chain_note` what the relationship actually is.

Explain the chain in `chain_note` in prose whichever rule you picked.

DO NOT SPLIT OUT SOMETHING THAT HAS ALREADY HAPPENED
A sub-question whose outcome is already settled is not a forecast — it is a fact, and in a
conjunction it contributes 1.0 while consuming a research slot that could have measured
something live. If the company has already filed, "will they file?" is not a
sub-question. Ask the thing that is still open, and fold what is settled into the
population a later step measures:

  bad   "Will they file an S-1?"                       (they filed in June)
  good  "Of companies that filed, how many completed
         the listing inside the same year?"

SEPARATE KNOWABLE FROM UNKNOWABLE (principle 2)
Label each sub-question:
  researchable  a base rate or historical frequency could be looked up for this
  judgment      no lookup exists; this needs an estimate

Be honest about the split. Labelling everything "judgment" defeats the purpose — if
nothing is researchable, the outside view has nothing to anchor on. Labelling
something researchable that has no real reference class is worse: it sends the next
step hunting for a rate that does not exist.

Give each sub-question a rough probability and a rationale. These are working estimates,
not the final answer — later steps will revise them against base rates and evidence.
"""


def build_decompose_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, Decomposition]:
    return Agent[ForecastDeps, Decomposition](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
        name="decompose",
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
    """Break the question into labelled sub-questions. No tools — this is pure analysis."""
    prompt = f"""Decompose this forecasting question.

{format_question(input)}{as_of_note(deps)}

Return a Decomposition."""

    agent = get_decompose_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            # No tools, so the ceiling is zero rather than whatever the process-wide
            # default happens to be. An agent that cannot search should not be holding a
            # search budget it could spend on a tool it does not have.
            budget=get_budget(agent.name),
            run_name="decompose",
        )
    return with_ids(result.output)


def with_ids(d: Decomposition) -> Decomposition:
    """Stamp `sq1`…`sqN` onto the sub-questions.

    Assigned here rather than asked for in the prompt: later steps point back at these
    ids, so they have to be unique and complete, and a model asked for keys will
    eventually hand back two `sq2`s or skip one.
    """
    return d.model_copy(
        update={
            "sub_questions": [
                s.model_copy(update={"id": f"sq{i}"})
                for i, s in enumerate(d.sub_questions, 1)
            ]
        }
    )
