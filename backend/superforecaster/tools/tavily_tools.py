"""Tavily search and page extraction. Both return the web as it stands today."""

from __future__ import annotations

from typing import Literal

from pydantic_ai import RunContext
from tavily import AsyncTavilyClient

from ..config import get_settings
from ..deps import ForecastDeps
from ..models import ResearchDoc, SourceRef

MAX_RESULTS = 5
MAX_CHUNKS_PER_SOURCE = 3
MAX_EXTRACT_URLS = 5
PAGE_CHARS = 2000
SEARCH_DEPTH = "basic"
_TIMEOUT = 15.0
_SLOW_TIMEOUT = 60.0


async def search_web(
    ctx: RunContext[ForecastDeps],
    query: str,
    topic: Literal["general", "news", "finance"] = "general",
) -> list[dict] | str:
    """Search the web. Wrap a phrase in double quotes to require it verbatim.
    Use `news` for current events, `finance` for markets and filings."""
    client = _client()
    if client is None:
        return "Web search unavailable: no TAVILY_API_KEY set."
    try:
        payload = await client.search(
            query,
            timeout=_TIMEOUT,
            search_depth=SEARCH_DEPTH,
            max_results=MAX_RESULTS,
            chunks_per_source=MAX_CHUNKS_PER_SOURCE,
            topic=topic,
            # Tavily errors when `exact_match` arrives without a quoted phrase.
            exact_match='"' in query,
        )
    except Exception as e:
        return f"Web search error: {e}"

    results = payload.get("results", [])
    _record(ctx, results, query=query, tool="search_web", body_key="content")
    if not results:
        return f"No web results for: {query}"
    return [{k: r[k] for k in ("title", "url", "content") if k in r} for r in results]


async def extract_pages(
    ctx: RunContext[ForecastDeps], urls: list[str], query: str | None = None
) -> list[dict] | str:
    """Read the full text of pages a search already returned. `query` reranks the
    extracted chunks by relevance."""
    wanted = [u for u in urls if u][:MAX_EXTRACT_URLS]
    if not wanted:
        return "No URLs given to extract."
    client = _client()
    if client is None:
        return "Page extraction unavailable: no TAVILY_API_KEY set."
    try:
        payload = await client.extract(
            wanted,
            extract_depth="basic",
            format="markdown",
            timeout=_SLOW_TIMEOUT,
            query=query,
        )
    except Exception as e:
        return f"Page extraction error: {e}"

    results = payload.get("results") or []
    _record(
        ctx,
        results,
        query=", ".join(wanted),
        tool="extract_pages",
        body_key="raw_content",
    )
    if not results:
        return f"Could not extract any of: {', '.join(wanted)}"
    return [
        {
            "title": r.get("title") or "",
            "url": r.get("url", ""),
            "text": (r.get("raw_content") or "")[:PAGE_CHARS],
        }
        for r in results
    ]


def _client() -> AsyncTavilyClient | None:
    # Built per call: `get_settings()` re-reads the environment so the key panel works
    # without a restart.
    key = get_settings().tavily_api_key
    return AsyncTavilyClient(api_key=key) if key else None


def _record(
    ctx: RunContext[ForecastDeps],
    results: list[dict],
    *,
    query: str,
    tool: str,
    body_key: str,
) -> None:
    """Note every URL for the citation check, and keep the text in the research store."""
    pages = [r for r in results if r.get("url")]
    ctx.deps.sources_seen.extend(
        SourceRef(url=r["url"], title=r.get("title") or "", query=query, tool=tool)
        for r in pages
    )
    store = ctx.deps.store
    if store is None or not pages:
        return
    docs = [
        ResearchDoc(
            url=r["url"],
            title=r.get("title") or "",
            body=(r.get(body_key) or "")[:PAGE_CHARS],
        )
        for r in pages
    ]
    try:
        store.remember(docs)
    except Exception:
        # A store that cannot be written is a lost convenience, not a lost search.
        pass
