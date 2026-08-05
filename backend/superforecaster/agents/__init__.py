"""One agent per methodology step.

Every module here has the same four things:

    INSTRUCTIONS            the system prompt
    build_<n>_agent(model)  construct
    get_<n>_agent()         lazy singleton, import-safe without API keys
    run_<n>(...)            the seam that graph nodes, tests, and evals all call

The uniformity is the point: a step you can call in isolation is a step you can
test in isolation. Agents know nothing about each other — sequencing lives in
`graphs`, and the methodology checks live in `checks`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from pydantic_ai import Agent, RunContext

from ..deps import ForecastDeps


def attach_budget_pressure(agent: Agent) -> None:
    """Register the cline instruction on a research agent.

    The second of the two channels that push a cell to converge. `tools._budget_notice`
    is the primary one — it lands at the moment the model decides whether to search
    again — but it has two blind spots this covers:

      - there is no tool result before the first request, so it cannot state the budget
        up front. The static `SEARCH BUDGET: at most N rounds` line this replaces did
        state it, but was frozen into request 1 and never updated afterwards.
      - after the last tool result it goes silent, while the model may still make several
        more requests: an output-validation retry, a text-only turn.

    Instructions are re-fetched per model request rather than once per run, which is what
    makes the pressure escalate *within* a run instead of being a fixed sentence.

    Registered on the lazy singleton at construction, so it appends once.
    """

    @agent.instructions
    def budget(ctx: RunContext[ForecastDeps]) -> str | None:
        b = getattr(ctx.deps, "budget", None)
        if b is None:
            return None
        if not b.past_the_cline:
            return (
                f"SEARCH BUDGET: {b.used} of {b.soft_depth} searches used. Prefer a few "
                "well-chosen searches over exhaustive looping."
            )
        return (
            "SEARCH BUDGET SPENT. Do not call another tool. Shorten your reasoning and "
            "return the structured answer now, from what you already found. Grade thin "
            "evidence as thin rather than searching for better."
        )


@contextmanager
def with_model(agent: Agent, deps: ForecastDeps) -> Iterator[Agent]:
    """Apply `deps.model` for the duration of one run.

    This is what lets the model garden swap models per question without rebuilding
    the agent. A no-op when `deps.model` is None, so production keeps using
    `config.resolve_agent_model()`.
    """
    if deps.model is None:
        yield agent
        return
    with agent.override(model=deps.model):
        yield agent


def format_question(input: Any) -> str:
    """The question block every agent prompt opens with.

    Shared so the four graph agents describe the question identically — a prompt
    difference between steps would be a silent source of disagreement.
    """
    return f"""QUESTION: {input.question}

RESOLUTION CRITERIA: {input.resolution_criteria}

RESOLUTION DATE: {input.resolution_date.isoformat()}

CATEGORY: {input.category}"""


def as_of_note(deps: ForecastDeps) -> str:
    """Tell the agent it is forecasting from a point in the past, when it is.

    Without this the model narrates in the present tense about a date years gone and
    treats an empty search as "nothing is happening" rather than "I am looking at an
    older world."
    """
    if deps.as_of is None:
        return ""
    return (
        f"\n\nIMPORTANT — YOU ARE FORECASTING AS OF {deps.as_of.date().isoformat()}.\n"
        "Your search tools return nothing published after that date. Reason only from "
        "what was knowable then. Do not use knowledge of what happened afterwards, and "
        "do not treat sparse results as evidence that nothing was happening."
    )
