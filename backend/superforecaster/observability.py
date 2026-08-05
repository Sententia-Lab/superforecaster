"""Agent observability — Logfire cloud traces and CLI progress logging."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterable
from typing import Any
from urllib.parse import urlparse

import httpx
import logfire
from pydantic_ai import Agent
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    TextPart,
    ThinkingPart,
)
from pydantic_ai.tools import RunContext

from config import get_settings, get_usage_limits
from pydantic_ai import UsageLimits

_logfire_configured = False
_warned_invalid_logfire_token = False
_cloud_tracing_active = False
_console_active = False


def _looks_like_logfire_write_token(token: str) -> bool:
    return token.startswith("pylf_v1_")


def logfire_tracing_enabled() -> bool:
    token = (get_settings().logfire_token or "").strip()
    return _looks_like_logfire_write_token(token)


def cloud_tracing_active() -> bool:
    return _cloud_tracing_active


def console_active() -> bool:
    return _console_active


def _logfire_base_url(token: str) -> str:
    parts = token.split("_")
    region = parts[2] if len(parts) > 2 else "us"
    return f"https://logfire-{region}.pydantic.dev"


def _token_is_valid(token: str) -> bool:
    try:
        response = httpx.get(
            f"{_logfire_base_url(token)}/v1/info",
            headers={"Authorization": token},
            timeout=5.0,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def configure_logfire(*, verbose: bool = False) -> None:
    global _logfire_configured, _warned_invalid_logfire_token, _cloud_tracing_active, _console_active
    if _logfire_configured:
        return

    settings = get_settings()
    token = (settings.logfire_token or "").strip()
    tracing = logfire_tracing_enabled() and _token_is_valid(token)

    if not tracing and not _warned_invalid_logfire_token:
        _warned_invalid_logfire_token = True
        print(
            "[logfire] Cloud tracing unavailable (no valid write token) — printing agent logs to the "
            "terminal instead. Create a write token at logfire.pydantic.dev → project → Settings → "
            "Write tokens and set LOGFIRE_TOKEN to enable cloud traces.",
            file=sys.stderr,
            flush=True,
        )

    kwargs: dict[str, Any] = {
        "service_name": "superforecaster",
        "scrubbing": False,
        "send_to_logfire": tracing,
        "console": (
            logfire.ConsoleOptions(min_log_level="info")
            if (verbose or not tracing)
            else False
        ),
    }
    if tracing:
        kwargs["token"] = token

    logfire.configure(**kwargs)
    if tracing:
        logfire.instrument_pydantic_ai(include_content=True, version=3)

    _cloud_tracing_active = tracing
    _console_active = bool(kwargs["console"])
    _logfire_configured = True


def _preview(value: Any, limit: int | None = 240) -> str:
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


# The single human-meaningful argument of each search tool. Three parameter names for
# the same idea, so the UI would otherwise have to know each tool's signature.
_QUERY_ARG_NAMES = ("query", "topic", "claim")


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
        return _preview(args, 200)
    return _preview(args, 200)


def _source_payload(ref: Any) -> dict[str, Any]:
    """A `SourceRef` as the UI's `source` event.

    `credibility` is None: nothing in the backend scores a domain today, and inventing
    a number to fill a coloured dot would be the UI lying with the server's authority.
    """
    url = getattr(ref, "url", "") or ""
    published = getattr(ref, "published_date", None)
    domain = urlparse(url).netloc
    return {
        "url": url,
        "domain": domain,
        # Falls back to the domain, then to the raw string. `SourceRef.title` used not
        # to exist, so this always reached the last branch — which printed an
        # unparseable URL in full as though it were a headline.
        "title": getattr(ref, "title", "") or domain or url,
        "query": getattr(ref, "query", ""),
        "published_date": published.isoformat() if published else None,
        "tool": getattr(ref, "tool", ""),
        "credibility": None,
    }


def _make_event_handler(*, verbose: bool):
    # No cloud sink to inspect the data in later, so print it all locally instead of a short preview.
    full = not cloud_tracing_active()
    show = verbose or full
    limit = None if full else 240

    async def _handler(
        ctx: RunContext[Any],
        stream: AsyncIterable[AgentStreamEvent],
    ) -> None:
        tool_n = 0
        emit = getattr(ctx.deps, "emit", None)
        # Which column of the grid this agent is filling, forwarded as an opaque tag.
        # Nothing here knows what a column *means* — that keeps the UI's routing key from
        # becoming something the observability layer has to model.
        sub_claim = getattr(ctx.deps, "sub_claim", None)
        # Tools append to `deps.sources_seen` themselves for the leakage audit. Diffing
        # that list is how a `source` event gets a real URL without touching any tool.
        # Safe under concurrency only because each cell is handed a private list.
        sources_reported = len(getattr(ctx.deps, "sources_seen", ()) or ())

        async for event in stream:
            if emit is not None:
                if isinstance(event, FunctionToolCallEvent):
                    emit(
                        "query",
                        {
                            "tool": event.part.tool_name,
                            "q": _tool_query_arg(event.part.args),
                            "hits": None,
                        },
                        sub_claim,
                    )
                elif isinstance(event, FunctionToolResultEvent):
                    seen = getattr(ctx.deps, "sources_seen", None) or []
                    for ref in seen[sources_reported:]:
                        emit("source", _source_payload(ref), sub_claim)
                    sources_reported = len(seen)
                elif isinstance(event, PartDeltaEvent):
                    # ToolCallPartDelta carries `args_delta`, not `content_delta`, so
                    # this picks up narration without leaking partial JSON arguments.
                    delta = getattr(event.delta, "content_delta", None)
                    if isinstance(delta, str) and delta:
                        emit("thought", {"delta": delta}, sub_claim)

            if isinstance(event, FunctionToolCallEvent):
                tool_n += 1
                logfire.info(
                    "agent chose tool {tool}",
                    tool=event.part.tool_name,
                    args=event.part.args,
                    step=tool_n,
                    _tags=["agent-progress", "tool-call"],
                )
                if show:
                    print(
                        f"[agent] tool call #{tool_n}: {event.part.tool_name}"
                        f"({_preview(event.part.args, limit)})",
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
                if show:
                    print(
                        f"[agent] tool result: {_preview(content, limit)}",
                        file=sys.stderr,
                        flush=True,
                    )
            elif isinstance(event, PartEndEvent):
                part = event.part
                if isinstance(part, TextPart) and part.content:
                    logfire.info(
                        "agent reasoning",
                        text=part.content,
                        _tags=["agent-progress", "reasoning"],
                    )
                    if show:
                        print(
                            f"[agent] model text: {_preview(part.content, limit)}",
                            file=sys.stderr,
                            flush=True,
                        )
                elif isinstance(part, ThinkingPart) and part.content:
                    logfire.info(
                        "agent thinking",
                        text=part.content,
                        _tags=["agent-progress", "thinking"],
                    )
                    if show:
                        print(
                            f"[agent] thinking: {_preview(part.content, limit)}",
                            file=sys.stderr,
                            flush=True,
                        )

    return _handler


async def run_agent(
    agent: Agent[Any, Any],
    prompt: str,
    *,
    deps: Any = None,
    verbose: bool = False,
    max_iterations: int | None = None,
    usage_limits: UsageLimits | None = None,
    run_name: str = "agent run",
) -> Any:
    """Run an agent with tracing, budget, and progress output.

    `deps` is forwarded to `agent.run` so tools can read the contamination clamps
    (`ForecastDeps.as_of`) and append to the leakage audit trail.
    """
    configure_logfire(verbose=verbose)
    limits = usage_limits or get_usage_limits(max_iterations=max_iterations)
    full = not cloud_tracing_active()
    show = verbose or full
    logging_active = cloud_tracing_active() or console_active()
    # A streamed run needs the handler attached even with tracing and console both off —
    # the UI is the sink in that case.
    streaming = getattr(deps, "emit", None) is not None
    trace_events = verbose or logging_active or streaming

    if show:
        print("[agent] starting run...", file=sys.stderr, flush=True)
        print(
            f"[agent] limits: {limits.request_limit} LLM requests, "
            f"{limits.tool_calls_limit} tool calls",
            file=sys.stderr,
            flush=True,
        )
        print(
            f"[agent] prompt: {_preview(prompt, None if full else 500)}",
            file=sys.stderr,
            flush=True,
        )
    if logging_active:
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
            deps=deps,
            usage_limits=limits,
            event_stream_handler=(
                _make_event_handler(verbose=verbose) if trace_events else None
            ),
        )

    if show or logging_active:
        usage = result.usage()
        logfire.info(
            "finished {run_name}",
            run_name=run_name,
            llm_requests=usage.requests,
            tool_calls=usage.tool_calls,
            total_tokens=usage.total_tokens,
            _tags=["agent-progress", "run-finish"],
        )
        if show:
            print(
                f"[agent] done: {usage.requests} LLM requests, {usage.tool_calls} tool calls, "
                f"{usage.total_tokens} tokens",
                file=sys.stderr,
                flush=True,
            )
            print(
                f"[agent] result: {_preview(result.output, None if full else 500)}",
                file=sys.stderr,
                flush=True,
            )

    return result
