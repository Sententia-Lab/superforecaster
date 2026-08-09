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

from genai_prices import Usage as PriceUsage, calc_price
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.usage import RunUsage

from ..deps import ForecastDeps


def spent_usd(model: Any, usage: RunUsage) -> float:
    """What this run has cost so far, from published per-token prices.

    Returns 0.0 when the price of a model is unknown — a test model, a provider
    `genai_prices` has no row for. An unpriced model must not stop a run, so the cost
    ceiling silently does not apply to one.
    """
    try:
        return float(
            calc_price(
                PriceUsage(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                ),
                model_ref=model.model_name,
                provider_id=model.system,
            ).total_price
        )
    except Exception:
        return 0.0


def attach_budget(agent: Agent) -> None:
    """Register the one instruction that tells an agent what it has left.

    Pydantic AI re-fetches instructions before **every** model request, so this runs once
    per iteration and `ctx.usage` is current when it does. That is the whole mechanism:
    the agent is told the remaining budget at each point it decides whether to spend more,
    rather than being handed a fixed sentence in request 1 that goes stale immediately.

    It is also where the cost ceiling is enforced. Pydantic AI counts requests, tool
    calls, and tokens, but not money; raising here stops the *next* request, before it is
    paid for.

    Registered on the lazy singleton at construction, so it appends once.

    **Never say "do not call another tool."** The structured answer is itself delivered as
    a tool call — pydantic-ai puts the output schema in the toolset — so an instruction
    against tool calls in general forbids the one thing that would end the run. A model
    that obeys it answers in plain text, pydantic-ai replies "please include your response
    in a tool call", the instruction is re-fetched and says the same thing again, and the
    run burns every request it has without ever producing output. Name the search tools,
    never tools as a category.
    """

    @agent.instructions
    def budget(ctx: RunContext[ForecastDeps]) -> str | None:
        b = getattr(ctx.deps, "budget", None)
        if b is None:
            return None

        spent = spent_usd(ctx.model, ctx.usage)
        if spent >= b.cost_usd:
            raise UsageLimitExceeded(
                f"{b.name} spent ${spent:.2f} of its ${b.cost_usd:.2f} cost limit"
            )

        left = (
            f"BUDGET LEFT — {b.iterations - ctx.usage.requests} of {b.iterations} turns, "
            f"{b.tokens - ctx.usage.total_tokens:,} of {b.tokens:,} tokens, "
            f"${b.cost_usd - spent:.2f} of ${b.cost_usd:.2f}."
        )
        if b.tool_calls == 0:
            return left

        searches = b.tool_calls - ctx.usage.tool_calls
        if searches > 0:
            return (
                f"{left} {searches} of {b.tool_calls} searches left. Prefer a few "
                "well-chosen searches over exhaustive looping."
            )
        return (
            f"{left} No searches left. Run no further searches — shorten your reasoning "
            "and return your structured answer now, from what you already found. "
            "Returning it is the only thing left to do, and it is still a tool call. "
            "Grade thin evidence as thin rather than searching for better."
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
