"""The one instruction that tells an agent what it has left, and the cost ceiling in it.

Pydantic AI re-fetches instructions before every model request, which is the whole
mechanism: the agent reads its remaining budget at each point it decides whether to spend
more. These tests drive a real `Agent` against `TestModel` and read the instructions back
off the recorded requests, so what the model was actually told is what gets asserted.
"""

from __future__ import annotations

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from config import Budget
from superforecaster.agents import attach_budget, spent_usd
from superforecaster.deps import ForecastDeps

RESEARCH = Budget(
    "test_cell", cost_usd=1.00, tokens=100_000, tool_calls=3, iterations=6
)
NO_TOOLS = Budget(
    "test_writer", cost_usd=1.00, tokens=100_000, tool_calls=0, iterations=4
)


def build(*, with_tool: bool = True) -> Agent:
    agent = Agent(
        TestModel(call_tools=["search"] if with_tool else []),
        deps_type=ForecastDeps,
    )

    if with_tool:

        @agent.tool
        async def search(ctx: RunContext[ForecastDeps], query: str) -> str:
            return "results"

    attach_budget(agent)
    return agent


async def instructions_per_request(agent: Agent, budget: Budget | None) -> list[str]:
    """What the model was told, once per model request, in order."""
    result = await agent.run("go", deps=ForecastDeps(budget=budget))
    return [
        m.instructions or ""
        for m in result.all_messages()
        if isinstance(m, ModelRequest)
    ]


# ---------- what the agent is told ----------


async def test_the_budget_line_names_all_four_ceilings():
    said = await instructions_per_request(build(), RESEARCH)

    assert "of 6 turns" in said[0]
    assert "of 100,000 tokens" in said[0]
    assert "of $1.00" in said[0]
    assert "3 of 3 searches left" in said[0]


async def test_the_numbers_count_down_between_iterations():
    """The point of the hook. A sentence written once into request 1 goes stale the moment
    the agent acts; this one is re-read every time it decides whether to act again."""
    said = await instructions_per_request(build(), RESEARCH)

    assert len(said) == 2
    assert "6 of 6 turns" in said[0]
    assert "3 of 3 searches left" in said[0]
    assert "5 of 6 turns" in said[1]
    assert "2 of 3 searches left" in said[1]


async def test_a_no_tool_agent_is_not_told_about_searches():
    """decompose, choose-lenses, reflect, synthesize and draft have no search tools.
    Telling one it has no searches left and to run no further searches invites it to stop
    before it returns its answer — which is itself delivered as a tool call."""
    said = await instructions_per_request(build(with_tool=False), NO_TOOLS)

    assert "BUDGET LEFT" in said[0]
    assert "search" not in said[0]


async def test_no_budget_means_no_budget_line():
    """`ForecastDeps()` arrives with `budget=None` from a test or a direct call."""
    said = await instructions_per_request(build(), None)

    assert "BUDGET LEFT" not in said[0]


# ---------- the cost ceiling ----------


async def test_an_overspent_run_stops_before_the_next_request(monkeypatch):
    """Pydantic AI counts requests, tool calls, and tokens, but not money. Raising from
    the instruction is what makes the fourth ceiling real, and it stops the *next* request
    rather than reporting an overrun after it was paid for."""
    monkeypatch.setattr("superforecaster.agents.spent_usd", lambda model, usage: 99.0)

    with pytest.raises(UsageLimitExceeded, match=r"test_cell spent \$99.00"):
        await build().run("go", deps=ForecastDeps(budget=RESEARCH))


def test_a_priced_model_is_costed_from_its_tokens():
    class Model:
        model_name = "claude-sonnet-4-6"
        system = "anthropic"

    assert spent_usd(Model(), RunUsage(input_tokens=1_000_000, output_tokens=0)) == 3.0


def test_an_unpriced_model_costs_nothing():
    """`TestModel`, and any provider `genai_prices` has no row for. An unknown price must
    not stop a run, so the cost ceiling silently does not apply to one."""

    class Model:
        model_name = "not-a-real-model"
        system = "nowhere"

    assert spent_usd(Model(), RunUsage(input_tokens=1_000_000, output_tokens=0)) == 0.0
