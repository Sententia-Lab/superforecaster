"""Agent observability — Logfire cloud traces and CLI progress logging."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterable
from typing import Any

import logfire
from pydantic_ai import Agent
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartStartEvent,
    TextPart,
    ThinkingPart,
)
from pydantic_ai.tools import RunContext

from config import get_settings, get_usage_limits
from pydantic_ai import UsageLimits

_logfire_configured = False
_warned_invalid_logfire_token = False


def _looks_like_logfire_write_token(token: str) -> bool:
    return token.startswith("pylf_v1_")


def logfire_tracing_enabled() -> bool:
    token = (get_settings().logfire_token or "").strip()
    return _looks_like_logfire_write_token(token)


def configure_logfire(*, verbose: bool = False) -> None:
    global _logfire_configured, _warned_invalid_logfire_token
    if _logfire_configured:
        return

    settings = get_settings()
    token = (settings.logfire_token or "").strip()
    tracing = logfire_tracing_enabled()

    kwargs: dict[str, Any] = {
        "service_name": "superforecaster",
        "scrubbing": False,
        "send_to_logfire": tracing,
        "console": logfire.ConsoleOptions(min_log_level="info") if verbose else False,
    }

    if token:
        if tracing:
            kwargs["token"] = token
        elif not _warned_invalid_logfire_token:
            _warned_invalid_logfire_token = True
            print(
                "[logfire] LOGFIRE_TOKEN is not a project write token (expected pylf_v1_...). "
                "Skipping cloud export. Use --verbose for local progress, or create a write token at "
                "logfire.pydantic.dev → project → Settings → Write tokens.",
                file=sys.stderr,
                flush=True,
            )

    logfire.configure(**kwargs)
    if tracing:
        logfire.instrument_pydantic_ai(include_content=True, version=3)
    _logfire_configured = True


def _preview(value: Any, limit: int = 240) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", " ")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _make_event_handler(*, verbose: bool):
    async def _handler(
        ctx: RunContext[Any],
        stream: AsyncIterable[AgentStreamEvent],
    ) -> None:
        tool_n = 0
        async for event in stream:
            if isinstance(event, FunctionToolCallEvent):
                tool_n += 1
                logfire.info(
                    "agent chose tool {tool}",
                    tool=event.part.tool_name,
                    args=event.part.args,
                    step=tool_n,
                    _tags=["agent-progress", "tool-call"],
                )
                if verbose:
                    print(
                        f"[agent] tool call #{tool_n}: {event.part.tool_name}"
                        f"({_preview(event.part.args)})",
                        file=sys.stderr,
                        flush=True,
                    )
            elif isinstance(event, FunctionToolResultEvent):
                content = getattr(event.result, "content", event.result)
                logfire.info(
                    "tool result for {tool}",
                    tool=getattr(event.result, "tool_name", "unknown"),
                    content=content,
                    _tags=["agent-progress", "tool-result"],
                )
                if verbose:
                    print(f"[agent] tool result: {_preview(content)}", file=sys.stderr, flush=True)
            elif isinstance(event, PartStartEvent):
                part = event.part
                if isinstance(part, TextPart) and part.content:
                    logfire.info(
                        "agent reasoning",
                        text=part.content,
                        _tags=["agent-progress", "reasoning"],
                    )
                    if verbose:
                        print(
                            f"[agent] model text: {_preview(part.content, 160)}",
                            file=sys.stderr,
                            flush=True,
                        )
                elif isinstance(part, ThinkingPart) and part.content:
                    logfire.info(
                        "agent thinking",
                        text=part.content,
                        _tags=["agent-progress", "thinking"],
                    )
                    if verbose:
                        print(
                            f"[agent] thinking: {_preview(part.content, 160)}",
                            file=sys.stderr,
                            flush=True,
                        )

    return _handler


async def run_agent(
    agent: Agent[Any, Any],
    prompt: str,
    *,
    verbose: bool = False,
    max_iterations: int | None = None,
    usage_limits: UsageLimits | None = None,
    run_name: str = "agent run",
) -> Any:
    configure_logfire(verbose=verbose)
    limits = usage_limits or get_usage_limits(max_iterations=max_iterations)
    trace_events = verbose or logfire_tracing_enabled()

    if verbose:
        print("[agent] starting run...", file=sys.stderr, flush=True)
        print(
            f"[agent] limits: {limits.request_limit} LLM requests, "
            f"{limits.tool_calls_limit} tool calls",
            file=sys.stderr,
            flush=True,
        )
    if logfire_tracing_enabled():
        logfire.info(
            "starting {run_name}",
            run_name=run_name,
            prompt_preview=_preview(prompt, 500),
            request_limit=limits.request_limit,
            tool_calls_limit=limits.tool_calls_limit,
            _tags=["agent-progress", "run-start"],
        )

    with logfire.span(run_name, prompt_preview=_preview(prompt, 500)):
        result = await agent.run(
            prompt,
            usage_limits=limits,
            event_stream_handler=_make_event_handler(verbose=verbose) if trace_events else None,
        )

    if verbose or logfire_tracing_enabled():
        usage = result.usage()
        logfire.info(
            "finished {run_name}",
            run_name=run_name,
            llm_requests=usage.requests,
            tool_calls=usage.tool_calls,
            total_tokens=usage.total_tokens,
            _tags=["agent-progress", "run-finish"],
        )
        if verbose:
            print(
                f"[agent] done: {usage.requests} LLM requests, {usage.tool_calls} tool calls, "
                f"{usage.total_tokens} tokens",
                file=sys.stderr,
                flush=True,
            )

    return result
