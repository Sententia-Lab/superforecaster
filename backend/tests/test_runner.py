"""`run_agent` itself — the function every agent goes through and nothing exercised.

Every other test in this suite monkeypatches `run_agent` away, so its body had no cover
at all. That is not academic: removing its `verbose` parameter left all eleven agents
passing `verbose=deps.verbose` into a signature that no longer accepted it, and the whole
suite stayed green because every caller was a stub taking `**kwargs`. Only a real agent
run found it.

`test_every_agent_calls_run_agent_with_arguments_it_accepts` is the cheap guard for that
class of bug; the rest exercise the body.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from superforecaster.config import Budget
from superforecaster.deps import ForecastDeps
from superforecaster.errors import AgentTimeout
from superforecaster.events import Query, Source, Thought
from superforecaster.models import SourceRef
from superforecaster.runner import run_agent

AGENTS_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "superforecaster" / "agents"
)

BUDGET = Budget("decompose", tool_calls=2, requests=4, tokens=100_000)


def _answers(text: str):
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return model


# ---------- the contract between the agents and the runner ----------


def test_every_agent_calls_run_agent_with_arguments_it_accepts():
    """An agent passing a keyword `run_agent` does not take is a TypeError at runtime.

    Checked by binding each call site's keywords against the real signature, because
    every test that runs an agent stubs `run_agent` with `**kwargs` — which accepts
    anything, including arguments that would fail in production.
    """
    signature = inspect.signature(run_agent)
    accepted = set(signature.parameters)
    offenders: list[str] = []

    for path in sorted(AGENTS_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "run_agent"):
                continue
            passed = {kw.arg for kw in node.keywords if kw.arg}
            unknown = passed - accepted
            if unknown:
                offenders.append(f"{path.name} passes {sorted(unknown)}")

    assert offenders == [], "; ".join(offenders)


# ---------- the body ----------


async def test_run_agent_returns_the_result_and_puts_the_budget_on_deps():
    """One place attaches the budget, so `withdraw_tools` can read it off `ctx.deps`."""
    seen: list[Budget | None] = []
    agent = Agent(FunctionModel(_answers("done")), deps_type=ForecastDeps)

    @agent.instructions
    def record(ctx) -> str:
        seen.append(ctx.deps.budget)
        return ""

    result = await run_agent(agent, "go", budget=BUDGET, deps=ForecastDeps())

    assert result.output == "done"
    assert seen == [BUDGET]


async def test_run_agent_does_not_mutate_the_deps_it_was_given():
    """It replaces the dataclass rather than writing to the caller's copy."""
    deps = ForecastDeps()
    agent = Agent(FunctionModel(_answers("done")), deps_type=ForecastDeps)

    await run_agent(agent, "go", budget=BUDGET, deps=deps)

    assert deps.budget is None


async def test_a_stalled_run_raises_agent_timeout_not_a_bare_timeout():
    """Callers degrade differently for "stopped responding" than for "acted too often"."""

    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(1)
        return ModelResponse(parts=[TextPart("late")])

    agent = Agent(FunctionModel(slow), deps_type=ForecastDeps)

    with pytest.raises(AgentTimeout) as exc:
        await run_agent(agent, "go", budget=BUDGET, deps=ForecastDeps(), timeout=0.05)

    assert "deadline" in str(exc.value)


# ---------- the events a subscriber receives ----------


async def test_a_tool_call_and_its_sources_reach_the_sink():
    """`Query` on the call, one `Source` per newly recorded ref, `Thought` on narration.

    The UI is built on these three. They used to be dicts shaped inside the tracing
    module; a subscriber now receives typed events and `app.stream` decides the wire
    format.
    """
    events: list[tuple[object, str | None]] = []
    calls = {"n": 0}

    async def search_then_answer(messages: list[ModelMessage], info: AgentInfo):
        # A sink puts the run in streaming mode, so this is the streamed shape: a tool
        # call on the first pass, narration on the second.
        calls["n"] += 1
        if calls["n"] == 1:
            yield {0: DeltaToolCall(name="search", json_args='{"query": "uk cpi"}')}
        else:
            yield "thinking "
            yield "out loud"

    agent = Agent(
        FunctionModel(stream_function=search_then_answer), deps_type=ForecastDeps
    )

    @agent.tool
    async def search(ctx, query: str) -> str:
        ctx.deps.sources_seen.append(
            SourceRef(url="https://ons.gov.uk/a", query=query, tool="search")
        )
        return "results"

    deps = ForecastDeps(emit=lambda e, sq: events.append((e, sq)), sub_question="sq1")
    await run_agent(agent, "go", budget=BUDGET, deps=deps)

    kinds = [type(e).__name__ for e, _ in events]
    assert {"Query", "Source", "Thought"} <= set(kinds)

    query = next(e for e, _ in events if isinstance(e, Query))
    assert (query.tool, query.text) == ("search", "uk cpi")

    source = next(e for e, _ in events if isinstance(e, Source))
    assert source.ref.url == "https://ons.gov.uk/a"

    # The opening chunk of each text part arrives as `PartStartEvent`, which the handler
    # does not read — only `PartDeltaEvent` — so the first fragment of every narration
    # never reaches the UI. Pre-existing (`observability.py:214` on main had the same
    # gap); pinned here so the behaviour is stated rather than assumed.
    thoughts = "".join(e.delta for e, _ in events if isinstance(e, Thought))
    assert thoughts == "out loud", "first chunk is dropped — known gap"

    assert {sq for _, sq in events} == {"sq1"}, "every event carries its column tag"


async def test_a_run_with_no_sink_still_completes():
    """`deps.emit` is None everywhere except a streamed run."""
    agent = Agent(FunctionModel(_answers("done")), deps_type=ForecastDeps)

    result = await run_agent(agent, "go", budget=BUDGET, deps=ForecastDeps())

    assert result.output == "done"
