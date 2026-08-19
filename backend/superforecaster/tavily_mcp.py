"""Web search through Tavily's hosted MCP server.

This is the production search path. It replaces the hand-rolled Tavily HTTP call in
`tools.search_web` for every run where `deps.as_of` is None.

A backtest still uses `tools.search_web`. The MCP server formats its results as text and
drops each result's `published_date`, so `tools._drop_leaked` — clamp 1 of ADR 17 — would
have nothing left to check. `web_search_toolset` picks the path per run, so a backtest
never reaches this module.

The server offers four tools. `tavily-search` is renamed to `search_web` so the prompts,
`SourceRef.tool`, and the frontend keep the one name they already know. Extract, map, and
crawl keep their own names and are offered on the live path only: none of them takes a date
filter, so each is an uncontrolled leak in a backtest.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from pydantic_ai import RunContext
from pydantic_ai.mcp import CallToolFunc, MCPToolset, ToolResult
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, RenamedToolset

from .config import get_settings
from .deps import ForecastDeps
from .models import SourceRef

SEARCH_TOOL = "tavily-search"

MAX_RESULTS = 5
MAX_CRAWL = 10
"""Ceilings imposed on the model's arguments.

The budget in `config.BUDGETS` counts tool calls, not tokens. One uncapped crawl returns
far more text than the five search results a call is priced against, so a single call could
spend a cell's whole token budget. The caps are applied in `process_tool_call`, where the
model cannot argue with them.
"""

_toolsets: dict[str, MCPToolset[ForecastDeps]] = {}


def _server_url(api_key: str) -> str:
    """The endpoint with the key attached.

    Tavily's hosted server takes the key as a query parameter. Built here on every call
    rather than held in a module constant, so a key set through `PUT /config/keys` takes
    effect without a restart, and so the secret is never a value some other module can
    read off an import.
    """
    base = get_settings().tavily_mcp_url
    return f"{base}?{urlencode({'tavilyApiKey': api_key})}"


def _toolset(api_key: str) -> MCPToolset[ForecastDeps]:
    """One long-lived connection per key.

    `MCPToolset` opens the connection lazily and holds it, so caching here is what stops
    every search from paying for a fresh TCP connect and `initialize` handshake.
    """
    if api_key not in _toolsets:
        _toolsets[api_key] = MCPToolset[ForecastDeps](
            _server_url(api_key),
            id="tavily",
            process_tool_call=process_tool_call,
        )
    return _toolsets[api_key]


# ---------- reading sources back out of a text result ----------

_TITLE = re.compile(r"^\s*Title:\s*(.+?)\s*$", re.MULTILINE)
_URL = re.compile(r"^\s*URL:\s*(\S+)\s*$", re.MULTILINE)


def _as_text(result: ToolResult) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        return "\n".join(_as_text(part) for part in result)  # type: ignore[arg-type]
    return str(result)


def parse_sources(text: str, *, query: str, tool: str) -> list[SourceRef]:
    """Recover one `SourceRef` per result from the server's formatted output.

    The server prints `Title:` and `URL:` on their own lines and prints no publication
    date, so `published_date` is None for every source this returns. Titles are matched to
    URLs by position, and a URL with no title ahead of it still gets a ref — an unnamed
    source that `check_citations` can verify beats a named one it cannot.
    """
    urls = _URL.findall(text)
    titles = _TITLE.findall(text)
    return [
        SourceRef(
            url=url,
            title=titles[i] if i < len(titles) else "",
            query=query,
            published_date=None,
            tool=tool,
        )
        for i, url in enumerate(urls)
    ]


def _requested_urls(args: dict[str, Any]) -> list[str]:
    """The URLs an extract, crawl, or map call was pointed at."""
    raw = args.get("urls") or args.get("url") or []
    values = raw if isinstance(raw, list) else [raw]
    return [v for v in values if isinstance(v, str) and v]


def _capped(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == SEARCH_TOOL:
        return {
            **args,
            "max_results": min(
                int(args.get("max_results") or MAX_RESULTS), MAX_RESULTS
            ),
            "search_depth": "basic",
            "include_raw_content": False,
            "include_images": False,
        }
    if name in ("tavily-crawl", "tavily-map"):
        return {**args, "limit": min(int(args.get("limit") or MAX_CRAWL), MAX_CRAWL)}
    return args


async def process_tool_call(
    ctx: RunContext[ForecastDeps],
    call_tool: CallToolFunc,
    name: str,
    args: dict[str, Any],
) -> ToolResult:
    """Cap the arguments on the way in, record the sources on the way out.

    Recording is not optional. `check_citations` fails a forecast that cites a URL absent
    from `deps.sources_seen`, and the run tree draws its source chips by diffing the same
    list, so an unrecorded search is a search that never happened as far as the rest of
    the system is concerned.
    """
    result = await call_tool(name, _capped(name, args))

    display_name = "search_web" if name == SEARCH_TOOL else name
    if name == SEARCH_TOOL:
        refs = parse_sources(
            _as_text(result), query=str(args.get("query", "")), tool=display_name
        )
    else:
        refs = [
            SourceRef(url=url, query=str(args.get("query", "")), tool=display_name)
            for url in _requested_urls(args)
        ]
    ctx.deps.sources_seen.extend(refs)
    return result


# ---------- the seam agents are built against ----------


def web_search_toolset(
    ctx: RunContext[ForecastDeps],
) -> AbstractToolset[ForecastDeps] | None:
    """The web tools this run gets, decided by whether it is a backtest.

    Returning None offers no web tools at all. That is the no-key case: an agent cannot
    call a tool it is not offered, so it spends nothing discovering the key is missing.
    `GET /config` already reports `search_enabled` to the operator.
    """
    from .tools import search_web

    api_key = get_settings().tavily_api_key
    if not api_key:
        return None
    if ctx.deps.as_of is not None:
        return FunctionToolset[ForecastDeps]([search_web])
    return RenamedToolset(_toolset(api_key), {"search_web": SEARCH_TOOL})


async def mcp_search(ctx: RunContext[ForecastDeps], query: str) -> str:
    """One search, called as a plain function rather than by the model.

    `tools.find_disconfirming_evidence` uses this to run three searches inside one tool
    call. It goes through `process_tool_call` for the same reason the model's own calls do:
    the sources have to land on `deps.sources_seen` either way.
    """
    api_key = get_settings().tavily_api_key
    if not api_key:
        return f"[Web search unavailable — no TAVILY_API_KEY set. Query was: {query}]"
    try:
        result = await process_tool_call(
            ctx, _toolset(api_key).direct_call_tool, SEARCH_TOOL, {"query": query}
        )
    except Exception as e:
        return f"Web search error: {e}"
    return _as_text(result)
