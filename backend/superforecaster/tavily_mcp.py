"""Web search through Tavily's hosted MCP server.

This is the production search path. It replaces the hand-rolled Tavily HTTP call in
`tools.search_web` for every run where `deps.as_of` is None.

A backtest still uses `tools.search_web`. No MCP tool returns a publication date, and search
accepts only `topic="general"` — the server rejects `"news"`, which is the mode that would
carry dates — so `tools._drop_leaked`, clamp 1 of ADR 17, would have nothing to check.
`web_search_toolset` picks the path per run, so a backtest never reaches this module.

The server offers five tools: `tavily_search`, `tavily_extract`, `tavily_crawl`,
`tavily_map`, and `tavily_research`. Search is renamed to `search_web` so the prompts,
`SourceRef.tool`, and the frontend keep the one name they already know. The other four keep
their own names and are offered on the live path only: none of them takes a date filter, so
each is an uncontrolled leak in a backtest.

**The names are underscored, and every one must match what the server serves.** A name that
does not match fails silently in three directions at once — the rename does not happen,
`_capped` does not cap, and `process_tool_call` records no sources — and nothing raises. Pin
them in tests against the literal string, never against the constant, or the test agrees with
the bug.

Every tool returns a JSON object, not text. Search puts its hits under `results`, research
puts its citations under `sources`. Neither carries a publication date.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from pydantic_ai import RunContext
from pydantic_ai.mcp import CallToolFunc, MCPToolset, ToolResult
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, RenamedToolset

from .config import get_settings
from .deps import ForecastDeps
from .models import SourceRef
from .tools import _parse_published, search_web

SEARCH_TOOL = "tavily_search"
RESEARCH_TOOL = "tavily_research"

_FORCED: dict[str, dict[str, Any]] = {
    SEARCH_TOOL: {
        "search_depth": "basic",
        "include_raw_content": False,
        "include_images": False,
    },
    # `model` defaults to "auto" on the server, and auto picked a tier that ran for 176
    # seconds against a 180-second agent timeout — one call from killing the step. "mini"
    # answered the same question in 37. Depth is our decision to make, not the model's:
    # a research tier is not something an agent should be able to escalate mid-run.
    RESEARCH_TOOL: {"model": "mini"},
    "tavily_extract": {"extract_depth": "basic", "include_images": False},
    "tavily_crawl": {"extract_depth": "basic"},
}

_BOUNDED: dict[str, dict[str, int]] = {
    SEARCH_TOOL: {"max_results": 5},
    "tavily_crawl": {"limit": 10, "max_depth": 1, "max_breadth": 10},
    "tavily_map": {"limit": 10, "max_depth": 1, "max_breadth": 10},
}
"""Ceilings imposed on the model's arguments, applied in `process_tool_call`.

The budget in `config.BUDGETS` counts tool calls, not tokens or seconds. One crawl left at
its server default of `limit=50` returns far more text than the five search results a call is
priced against, and one research call at `model="auto"` can outlast the agent timeout. Both
are one tool call as far as the budget is concerned, so the ceiling has to live here.

`_FORCED` overwrites whatever the model asked for. `_BOUNDED` takes the smaller of the two,
so an agent may still ask for less.
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


# ---------- reading sources back out of a result ----------

_SOURCE_KEYS = ("results", "sources")
"""Where each tool puts the pages it saw. Search uses `results`, research uses `sources`."""


def _as_text(result: ToolResult) -> str:
    """The result as something a model can read.

    Every Tavily tool answers with a JSON object, so this is `json.dumps` in practice.
    `str()` on a dict would hand the model a Python repr with single quotes.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        return "\n".join(_as_text(part) for part in result)  # type: ignore[arg-type]
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, default=str)
    return str(result)


def _as_dict(result: ToolResult) -> dict[str, Any]:
    """The result as a mapping, or an empty one when it is not JSON at all."""
    if isinstance(result, dict):
        return result
    try:
        parsed = json.loads(_as_text(result))
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_sources(result: ToolResult, *, query: str, tool: str) -> list[SourceRef]:
    """One `SourceRef` per page the tool reports having read.

    `published_date` is read rather than assumed absent. No Tavily MCP tool returns one
    today — search only accepts `topic="general"`, and the server rejects `"news"`, which is
    the mode that would carry dates — but reading the field costs nothing and stops this
    from being a second place to fix if that changes.

    Returns an empty list for a body with no recognizable sources. The caller falls back to
    the URLs the call was pointed at, so a crawl still records something.
    """
    data = _as_dict(result)
    items = next(
        (data[key] for key in _SOURCE_KEYS if isinstance(data.get(key), list)), []
    )
    refs: list[SourceRef] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if not url:
            continue
        refs.append(
            SourceRef(
                url=url,
                title=item.get("title") or "",
                query=query,
                published_date=_parse_published(item.get("published_date")),
                tool=tool,
            )
        )
    return refs


def _requested_urls(args: dict[str, Any]) -> list[str]:
    """The URLs an extract, crawl, or map call was pointed at."""
    raw = args.get("urls") or args.get("url") or []
    values = raw if isinstance(raw, list) else [raw]
    return [v for v in values if isinstance(v, str) and v]


def _capped(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """The arguments the server is actually sent."""
    bounded = {}
    for key, ceiling in _BOUNDED.get(name, {}).items():
        try:
            asked = int(args.get(key) or ceiling)
        except (TypeError, ValueError):
            asked = ceiling
        bounded[key] = min(asked, ceiling)
    return {**args, **_FORCED.get(name, {}), **bounded}


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
    query = str(args.get("query") or args.get("input") or "")
    refs = parse_sources(result, query=query, tool=display_name)
    if not refs:
        # A crawl or map reports its pages in a shape this does not read. The URLs it was
        # pointed at are still sources the agent saw, and an approximate audit trail beats
        # an empty one.
        refs = [
            SourceRef(url=url, query=query, tool=display_name)
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
