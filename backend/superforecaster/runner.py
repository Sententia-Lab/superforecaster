"""Running one agent: model, budget, deadline, span, and typed progress events.
This module instruments but never configures Logfire — `app.observability` does."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterable
from dataclasses import replace
from typing import Any

import logfire
from pydantic_ai import Agent
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
)
from pydantic_ai.tools import RunContext

from .config import Budget, get_agent_timeout, get_model_settings, resolve_agent_model
from .deps import ForecastDeps
from .errors import AgentTimeout
from .events import Query, Sink, Source, Thought

_QUERY_ARG_NAMES = ("query", "topic", "claim", "url", "urls")


def preview(value: Any, limit: int | None = 240) -> str:
    """A value as one short line, for span attributes."""
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    if limit is None:
        return text
    text = text.replace("\n", " ")
    return text[:limit] + "..." if len(text) > limit else text


def _tool_query_arg(args: Any) -> str:
    """The human-meaningful argument of a tool call, for display."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return args
    if isinstance(args, dict):
        for name in _QUERY_ARG_NAMES:
            value = args.get(name)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, list) and value:
                return ", ".join(str(v) for v in value)
    return preview(args, 200)


def _make_event_handler(sink: Sink, sub_question: str | None):
    """Turn the agent's event stream into `Query`/`Source`/`Thought` events.

    Sources are detected by diffing `deps.sources_seen` after each tool result, which
    is why every research cell gets a private list.
    """

    async def _handler(
        ctx: RunContext[Any], stream: AsyncIterable[AgentStreamEvent]
    ) -> None:
        reported = len(ctx.deps.sources_seen)
        async for event in stream:
            if isinstance(event, FunctionToolCallEvent):
                sink(
                    Query(
                        tool=event.part.tool_name, text=_tool_query_arg(event.part.args)
                    ),
                    sub_question,
                )
            elif isinstance(event, FunctionToolResultEvent):
                seen = ctx.deps.sources_seen
                for ref in seen[reported:]:
                    sink(Source(ref=ref), sub_question)
                reported = len(seen)
            elif isinstance(event, PartDeltaEvent):
                delta = getattr(event.delta, "content_delta", None)
                if isinstance(delta, str) and delta:
                    sink(Thought(delta=delta), sub_question)

    return _handler


def _deadline(seconds: float):
    """`asyncio.timeout(seconds)`, or nothing when timeouts are switched off."""
    return (
        asyncio.timeout(seconds)
        if seconds and seconds > 0
        else contextlib.nullcontext()
    )


def search_note(budget: Budget) -> str:
    """The one static sentence about the search budget, appended to the user prompt.
    Static so the prompt prefix caches; `withdraw_tools` enforces it (ADR 81)."""
    return (
        f"\n\nYou may make at most {budget.tool_calls} tool calls. When they run out the "
        "search tools are withdrawn; return your structured answer from what you have."
    )


async def run_agent(
    agent: Agent[Any, Any],
    prompt: str,
    *,
    budget: Budget,
    deps: ForecastDeps | None = None,
    model: str | None = None,
    timeout: float | None = None,
    run_name: str = "agent run",
) -> Any:
    """Run an agent with a model, a budget, a deadline, and tracing.

    The budget rides on a copy of `deps` so `withdraw_tools` can read it on every
    request. The deadline catches a provider call that never returns, which no usage
    limit would.
    """
    deps = replace(deps or ForecastDeps(), budget=budget)
    if budget.tool_calls > 0:
        prompt += search_note(budget)
    deadline = get_agent_timeout() if timeout is None else timeout
    sink = deps.emit

    logfire.info(
        "starting {run_name}",
        run_name=run_name,
        prompt_preview=preview(prompt, 500),
        budget=budget.name,
        _tags=["agent-progress", "run-start"],
    )
    cancelled: asyncio.CancelledError | None = None
    with logfire.span(run_name, prompt_preview=preview(prompt, 500)) as span:
        try:
            async with _deadline(deadline):
                result = await agent.run(
                    prompt,
                    deps=deps,
                    # Only choose a model for an agent built without one, so a test's
                    # `FunctionModel` and an eval's override are left alone.
                    model=model
                    or (resolve_agent_model() if agent.model is None else None),
                    model_settings=get_model_settings(),
                    usage_limits=budget.limits(),
                    event_stream_handler=(
                        _make_event_handler(sink, deps.sub_question) if sink else None
                    ),
                )
        except asyncio.CancelledError as exc:
            # The client hung up (ADR 46). Close the span cleanly, then re-raise.
            span.set_attribute("cancelled", True)
            cancelled = exc
        except TimeoutError as exc:
            raise AgentTimeout(
                f"{run_name} exceeded its {deadline:g}s deadline"
            ) from exc
    if cancelled is not None:
        raise cancelled

    usage = result.usage
    logfire.info(
        "finished {run_name}",
        run_name=run_name,
        llm_requests=usage.requests,
        tool_calls=usage.tool_calls,
        total_tokens=usage.total_tokens,
        result_preview=preview(result.output, 500),
        _tags=["agent-progress", "run-finish"],
    )
    return result
