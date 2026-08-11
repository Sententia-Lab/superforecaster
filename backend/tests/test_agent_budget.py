"""The one instruction that tells an agent what it has left, and the cost ceiling in it.

Pydantic AI re-fetches instructions before every model request, which is the whole
mechanism: the agent reads its remaining budget at each point it decides whether to spend
more. These tests drive a real `Agent` against `TestModel` and read the instructions back
off the recorded requests, so what the model was actually told is what gets asserted.
"""

from __future__ import annotations

import pytest
from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage, UsageLimits

from pydantic import BaseModel

from superforecaster.config import Budget
from superforecaster.agents import (
    SEARCH_RESERVE,
    attach_budget,
    spent_usd,
    withdraw_spent_tools,
)
from superforecaster.deps import ForecastDeps

RESEARCH = Budget(
    "test_cell", cost_usd=1.00, tokens=100_000, tool_calls=3, iterations=6
)
NO_TOOLS = Budget(
    "test_writer", cost_usd=1.00, tokens=100_000, tool_calls=0, iterations=4
)
# Big enough that a batch of four still leaves the withdrawal something to do.
CELL = Budget("test_big", cost_usd=1.00, tokens=100_000, tool_calls=8, iterations=25)


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


def _always_searches(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A model that never stops. It is the case the ceilings exist for."""
    return ModelResponse(parts=[ToolCallPart("search", {"query": "again"})])


async def instructions_until_exhausted(budget: Budget) -> list[str]:
    """What a greedy agent was told, request by request, on its way into the ceiling.

    Runs with the budget's own `limits()`, the way `runner.run_agent` does — the
    endgame only exists when Pydantic AI is enforcing the ceiling, so a run without them
    tests nothing about it.
    """
    agent = Agent(FunctionModel(_always_searches), deps_type=ForecastDeps)

    @agent.tool
    async def search(ctx: RunContext[ForecastDeps], query: str) -> str:
        return "results"

    attach_budget(agent)

    with capture_run_messages() as messages:
        with pytest.raises(UsageLimitExceeded):
            await agent.run(
                "go", deps=ForecastDeps(budget=budget), usage_limits=budget.limits()
            )
    return [m.instructions or "" for m in messages if isinstance(m, ModelRequest)]


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


# ---------- the endgame ----------


async def test_the_order_to_stop_arrives_while_a_search_is_still_legal():
    """The failure these exist to prevent.

    Told only at zero, an agent gets exactly one turn to comply and the turn after it is
    fatal: `tool_calls_limit` refuses the call *before* the tool runs, so the cell returns
    nothing and its column falls back to a pre-research guess. A warning delivered at the
    cliff edge is not a warning.
    """
    said = await instructions_until_exhausted(RESEARCH)

    stops = [i for i, s in enumerate(said) if "Stop searching now" in s]
    spent = [i for i, s in enumerate(said) if "No searches left" in s]

    assert stops, "never told to stop while it could still act on being told"
    assert min(stops) < min(spent), "the order to stop arrived only at the cliff edge"


async def test_the_three_search_bands_in_order():
    """`RESEARCH` allows 3 searches and the reserve is 2, so the agent is encouraged
    once, ordered to land twice, and then told it is out."""
    assert RESEARCH.tool_calls == 3 and SEARCH_RESERVE == 2
    said = await instructions_until_exhausted(RESEARCH)

    assert "3 of 3 searches left. Prefer" in said[0]
    assert "Only 2 of 3 searches left. Stop searching now" in said[1]
    assert "Only 1 of 3 searches left. Stop searching now" in said[2]
    assert "No searches left" in said[3]


async def test_every_band_that_says_stop_names_searching_and_not_tools():
    """ADR 62. The structured answer is itself delivered as a tool call, so an
    instruction against tool calls *as a category* forbids the one act that would end the
    run — the agent answers in plain text, is asked to use a tool, reads the same
    instruction again, and burns every request it has."""
    said = await instructions_until_exhausted(RESEARCH)

    for line in said:
        if "Stop" in line or "no further" in line:
            assert "search" in line.lower()
            assert "no further tools" not in line.lower()


# ---------- the tools are withdrawn, not merely discouraged ----------


def _ctx(deps: ForecastDeps, tool_calls: int):
    """The bit of `RunContext` `withdraw_spent_tools` reads: deps and usage."""

    class Ctx:
        pass

    ctx = Ctx()
    ctx.deps = deps
    ctx.usage = RunUsage(tool_calls=tool_calls)
    return ctx


class _Answer(BaseModel):
    finding: str


def _greedy(batch: int):
    """Searches in batches of `batch` for as long as a search tool is offered.

    `batch=1` is the model that ignores every warning it is given. `batch=4` is the model
    that never sees one: four per turn walks a budget of 8 straight past every band.
    """

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(t.name == "search" for t in info.function_tools):
            return ModelResponse(
                parts=[ToolCallPart("search", {"query": f"q{i}"}) for i in range(batch)]
            )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"finding": "thin"})]
        )

    return model


def _spender(batch: int, *, withdraw: bool) -> Agent:
    agent = Agent(
        FunctionModel(_greedy(batch)),
        deps_type=ForecastDeps,
        output_type=_Answer,
        prepare_tools=withdraw_spent_tools if withdraw else None,
    )

    @agent.tool
    async def search(ctx: RunContext[ForecastDeps], query: str) -> str:
        return "results"

    attach_budget(agent)
    return agent


@pytest.mark.parametrize("batch", [1, 3, 4])
async def test_a_greedy_agent_still_returns_an_answer(batch: int):
    """The whole point. A model that cannot see a search tool cannot call one, and the
    output tool is exempt from `tool_calls_limit`, so answering is always still available.

    Every batch size here defeats the prompt. At 1 the agent is told to stop three times
    and searches anyway; at 4 it is never told at all, walking 8 -> 4 -> spent."""
    result = await _spender(batch, withdraw=True).run(
        "go", deps=ForecastDeps(budget=CELL), usage_limits=CELL.limits()
    )

    assert result.output.finding == "thin"
    # It may overshoot by one over-eager batch, never by a second.
    assert result.usage().tool_calls < CELL.tool_calls + batch


@pytest.mark.parametrize("batch", [1, 3, 4])
async def test_without_withdrawal_the_same_agent_loses_everything(batch: int):
    """What the fix is worth. `UsageLimitExceeded` is raised before the tool runs, so
    there is no partial output to salvage — the cell contributes nothing at all."""
    with pytest.raises(UsageLimitExceeded):
        await _spender(batch, withdraw=False).run(
            "go",
            deps=ForecastDeps(budget=CELL),
            usage_limits=UsageLimits(tool_calls_limit=CELL.tool_calls),
        )


async def test_the_enforced_ceiling_leaves_room_for_one_overshooting_batch():
    """Pydantic AI refuses a batch whole. Without headroom, a model that asks for four
    searches with two left dies one turn from writing up, having done nothing wrong that
    the budget line could have warned it about."""
    assert CELL.limits().tool_calls_limit == CELL.tool_calls * 2


async def test_an_agent_with_no_budget_keeps_every_tool():
    """A direct call or a test that passes no budget must behave as it always did."""
    offered = await withdraw_spent_tools(
        _ctx(ForecastDeps(budget=None), tool_calls=99), ["search"]
    )
    assert offered == ["search"]


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
