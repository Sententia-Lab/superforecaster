"""The search budget: a static note in the prompt, `UsageLimits`, and tool withdrawal."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage, UsageLimits

from superforecaster.agents import withdraw_tools
from superforecaster.config import Budget
from superforecaster.deps import ForecastDeps
from superforecaster.runner import search_note

CELL = Budget("test_big", tool_calls=8, requests=25, tokens=100_000)


def _ctx(deps: ForecastDeps, tool_calls: int):
    class Ctx:
        pass

    ctx = Ctx()
    ctx.deps = deps
    ctx.usage = RunUsage(tool_calls=tool_calls)
    return ctx


class _Answer(BaseModel):
    finding: str


def _greedy(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Searches for as long as a search tool is offered, then answers."""
    if any(t.name == "search" for t in info.function_tools):
        return ModelResponse(parts=[ToolCallPart("search", {"query": "again"})])
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"finding": "thin"})]
    )


def _spender(*, withdraw: bool) -> Agent:
    agent = Agent(
        FunctionModel(_greedy),
        deps_type=ForecastDeps,
        output_type=_Answer,
        capabilities=[Hooks(prepare_tools=withdraw_tools)] if withdraw else [],
    )

    @agent.tool
    async def search(ctx: RunContext[ForecastDeps], query: str) -> str:
        return "results"

    return agent


async def test_a_greedy_agent_still_returns_an_answer():
    """A model cannot call a tool it is not offered, and the output tool is exempt
    from `tool_calls_limit`, so answering is always still available."""
    result = await _spender(withdraw=True).run(
        "go", deps=ForecastDeps(budget=CELL), usage_limits=CELL.limits()
    )

    assert result.output.finding == "thin"
    assert result.usage.tool_calls == CELL.tool_calls


async def test_without_withdrawal_the_same_agent_loses_everything():
    with pytest.raises(UsageLimitExceeded):
        await _spender(withdraw=False).run(
            "go",
            deps=ForecastDeps(budget=CELL),
            usage_limits=UsageLimits(tool_calls_limit=CELL.tool_calls),
        )


def test_the_search_note_names_the_budget():
    assert "at most 8 tool calls" in search_note(CELL)


async def test_an_agent_with_no_budget_keeps_every_tool():
    offered = await withdraw_tools(
        _ctx(ForecastDeps(budget=None), tool_calls=99), ["search"]
    )
    assert offered == ["search"]


async def test_the_tavily_tools_go_away_without_a_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    offered = await withdraw_tools(
        _ctx(ForecastDeps(budget=CELL), tool_calls=0),
        ["search_web", "extract_pages", "search_wikipedia", "search_research"],
    )

    assert offered == ["search_wikipedia", "search_research"]


async def test_every_tool_is_offered_when_the_key_is_set(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    offered = await withdraw_tools(
        _ctx(ForecastDeps(budget=CELL), tool_calls=0),
        ["search_web", "search_wikipedia"],
    )

    assert offered == ["search_web", "search_wikipedia"]


async def test_a_spent_budget_withdraws_everything(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")

    offered = await withdraw_tools(
        _ctx(ForecastDeps(budget=CELL), tool_calls=CELL.tool_calls),
        ["search_web", "search_wikipedia", "search_research"],
    )

    assert offered == []


def test_a_budget_scales_every_ceiling_together():
    deeper = CELL.scaled(10)
    assert (deeper.tool_calls, deeper.requests, deeper.tokens) == (16, 50, 200_000)
