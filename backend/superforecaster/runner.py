"""Running one agent: a budget, a deadline, a span, and typed progress events.

This module instruments; it does not configure. It opens Logfire spans and writes
Logfire logs, and if nothing has called `logfire.configure()` those go nowhere at no
cost. Deciding whether traces are sent, and where, belongs to the application —
`app.observability` does it.

What used to live here as well: reading the Logfire token, probing it over HTTP,
printing progress to stderr, and building the browser's SSE payloads. A library that
must do all four before it can call a model is not a library.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterable
from dataclasses import is_dataclass, replace
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

from .config import Budget, get_agent_timeout
from .errors import AgentTimeout
from .events import Query, Sink, Source, Thought

# The single human-meaningful argument of each search tool. Three parameter names for
# the same idea, so a subscriber would otherwise have to know each tool's signature.
_QUERY_ARG_NAMES = ("query", "topic", "claim")


def preview(value: Any, limit: int | None = 240) -> str:
    """A value as one short line. Used for span attributes, and by the CLI's printer."""
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    if limit is None:
        return text
    text = text.replace("\n", " ")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _tool_query_arg(args: Any) -> str:
    """The query a tool call is asking about, for display.

    Tool args arrive as a JSON string or a dict depending on the provider, and a call
    that fails to parse still deserves a readable label rather than an exception.
    """
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
        return preview(args, 200)
    return preview(args, 200)


def _make_event_handler(sink: Sink, sub_question: str | None):
    """Turn the agent's event stream into `AgentEvent`s for one subscriber.

    Attached only when `deps.emit` is set, because attaching it is what puts the run in
    streaming mode — a cost with no payer when nobody is listening. Tracing does not
    need it: `logfire.instrument_pydantic_ai` records tool calls and model messages on
    its own, and duplicating that here is how this module used to end up asking the
    application whether tracing was on.
    """

    async def _handler(
        ctx: RunContext[Any],
        stream: AsyncIterable[AgentStreamEvent],
    ) -> None:
        # Tools append to `deps.sources_seen` themselves for the leakage audit. Diffing
        # that list is how a `Source` event gets a real URL without touching any tool.
        # Safe under concurrency only because each cell is handed a private list.
        sources_reported = len(getattr(ctx.deps, "sources_seen", ()) or ())

        async for event in stream:
            if isinstance(event, FunctionToolCallEvent):
                sink(
                    Query(
                        tool=event.part.tool_name,
                        text=_tool_query_arg(event.part.args),
                    ),
                    sub_question,
                )
            elif isinstance(event, FunctionToolResultEvent):
                seen = getattr(ctx.deps, "sources_seen", None) or []
                for ref in seen[sources_reported:]:
                    sink(Source(ref=ref), sub_question)
                sources_reported = len(seen)
            elif isinstance(event, PartDeltaEvent):
                # ToolCallPartDelta carries `args_delta`, not `content_delta`, so this
                # picks up narration without leaking partial JSON arguments.
                delta = getattr(event.delta, "content_delta", None)
                if isinstance(delta, str) and delta:
                    sink(Thought(delta=delta), sub_question)

    return _handler


def _deadline(seconds: float):
    """`asyncio.timeout(seconds)`, or a no-op when timeouts are switched off.

    Zero or negative means disabled — the escape hatch for a long backtest run, and the
    reason this is a helper rather than an `async with asyncio.timeout(...)` inline.
    """
    if seconds and seconds > 0:
        return asyncio.timeout(seconds)
    return contextlib.nullcontext()


async def run_agent(
    agent: Agent[Any, Any],
    prompt: str,
    *,
    budget: Budget,
    deps: Any = None,
    timeout: float | None = None,
    run_name: str = "agent run",
) -> Any:
    """Run an agent with tracing, a budget, and a deadline.

    `deps` is forwarded to `agent.run` so tools can read the contamination clamps
    (`ForecastDeps.as_of`) and append to the leakage audit trail. The budget is attached
    to that copy so `agents.attach_budget`'s instruction can read it back off `ctx.deps`
    on every model request — one place puts it there, rather than every call site.

    Two independent ceilings, because there are two ways to never finish. The budget
    bounds how much the agent may spend. The deadline bounds how long one act may take,
    and it is the one that matters for a stuck browser: a provider request that never
    returns reaches no limit, raises nothing, and leaves every subscriber waiting on an
    `end` frame that is never sent.
    """
    limits = budget.limits()
    if is_dataclass(deps) and not isinstance(deps, type):
        deps = replace(deps, budget=budget)
    deadline = get_agent_timeout() if timeout is None else timeout

    logfire.info(
        "starting {run_name}",
        run_name=run_name,
        prompt_preview=preview(prompt, 500),
        budget_name=budget.name,
        request_limit=budget.iterations,
        tool_calls_limit=budget.tool_calls,
        total_tokens_limit=budget.tokens,
        cost_limit_usd=budget.cost_usd,
        _tags=["agent-progress", "run-start"],
    )

    cancelled: asyncio.CancelledError | None = None
    with logfire.span(run_name, prompt_preview=preview(prompt, 500)) as span:
        try:
            async with _deadline(deadline):
                sink = getattr(deps, "emit", None)
                result = await agent.run(
                    prompt,
                    deps=deps,
                    usage_limits=limits,
                    event_stream_handler=(
                        _make_event_handler(sink, getattr(deps, "sub_question", None))
                        if sink is not None
                        else None
                    ),
                )
        except asyncio.CancelledError as exc:
            # The client hung up mid-run — the deliberate stop ADR 46 promises, not a
            # failure. Exit the span cleanly (held until after the `with`, so the trace
            # does not record an unhandled exception and the errors view shows only real
            # ones), then let the cancellation keep propagating to `machine.execute_step`,
            # which records the step as `error='cancelled'`.
            span.set_attribute("cancelled", True)
            logfire.info(
                "{run_name} cancelled — client disconnected",
                run_name=run_name,
                _tags=["agent-progress", "run-cancelled"],
            )
            cancelled = exc
        except TimeoutError as exc:
            # Raised as our own type so callers can tell "stopped responding" from
            # "acted too many times" — they degrade differently, and a bare TimeoutError
            # from somewhere inside httpx would be indistinguishable from either.
            message = f"{run_name} exceeded its {deadline:g}s deadline"
            logfire.error(
                "{run_name} timed out",
                run_name=run_name,
                timeout_seconds=deadline,
                _tags=["agent-progress", "run-timeout"],
            )
            raise AgentTimeout(message) from exc

    if cancelled is not None:
        raise cancelled

    usage = result.usage()
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
