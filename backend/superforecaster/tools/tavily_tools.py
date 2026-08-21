"""The four Tavily endpoints, one tool each, over the `tavily-python` SDK.

| Tool | `AsyncTavilyClient` call | Answers |
|---|---|---|
| `search_web` | `.search()` | which pages discuss this |
| `extract_pages` | `.extract()` | what a page you already found says |
| `crawl_site` | `.crawl()` | what a site says across several pages |
| `map_site` | `.map()` | what pages a site has, without reading them |

**Every tool returns the web as it stands today.** None of them takes a date, so none is
usable for backtesting — see ADR 17 for why the date clamp that used to live here was
removed rather than repaired.

The agent chooses `query` and `topic`. Every other `POST /search` parameter is set here,
because `config.BUDGETS` counts calls rather than tokens and one call must not spend a
cell's whole budget: `max_results` is `MAX_RESULTS`, `chunks_per_source` is
`MAX_CHUNKS_PER_SOURCE`, and `timeout` is `_TIMEOUT`.

`extract_pages`, `crawl_site`, and `map_site` keep constant depth and breadth.

Every tool answers with JSON, built by `_json` from the dicts Tavily already returned.
None of them renders text, so there is no format to keep in step with the API.

Every URL an agent sees is recorded on `ctx.deps.sources_seen`, so a run can be audited for
leakage rather than trusted.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic_ai import RunContext
from tavily import AsyncTavilyClient

from ..config import get_settings
from ..deps import ForecastDeps
from ..models import ResearchDoc, SourceRef
from .dates import _as_utc, _parse_published

MAX_RESULTS = 5
MAX_CHUNKS_PER_SOURCE = 3
MAX_EXTRACT_URLS = 5
MAX_CRAWL_PAGES = 10
MAX_MAP_LINKS = 20
CRAWL_DEPTH = 5
_PAGE_CHARS = 2000

SEARCH_DEPTH = "basic"

_TIMEOUT = 15.0
_SLOW_TIMEOUT = 60.0
"""Extract, crawl, and map fetch whole pages, so they are slower than a search. Both sit
inside `DEFAULT_AGENT_TIMEOUT_SECONDS` — a tool that outlives the agent kills the step."""

# Tools


async def search_web(
    ctx: RunContext[ForecastDeps],
    query: str,
    topic: Literal["general", "news", "finance"] = "general",
) -> str:
    """Search the web for current information and reporting.

    Returns the top results as text, or a message explaining why none are available.
    Missing results mean missing information, not an error — say so in your reasoning
    rather than treating it as a failure.

    Args:
        query: What to search for. Write it the way you would type it into a search engine. Wrap a phrase in double quotes to require it verbatim (e.g. "John Smith" CEO Acme Corp) — results that do not contain the quoted phrase are dropped. Quote a phrase only when a near match is no use to you, because requiring one narrows the results.
        topic: The category of the search. News is useful for retrieving real-time updates, particularly about politics, sports, and major current events covered by mainstream media sources. Finance covers markets, company results, and filings. General is for broader, more general-purpose searches that may include a wide range of sources.

    """
    client = _client()
    if client is None:
        return _unavailable("Web search", query)

    try:
        payload = await client.search(
            query,
            timeout=_TIMEOUT,
            search_depth=SEARCH_DEPTH,
            max_results=MAX_RESULTS,
            chunks_per_source=MAX_CHUNKS_PER_SOURCE,
            include_favicon=True,
            include_usage=True,
            topic=topic,
            # Not a parameter, because the two halves have to agree: Tavily errors when
            # `exact_match` arrives without a quoted phrase, and ignores the quotes
            # without it. Reading it off the query keeps the promise the `query`
            # description makes and puts the error out of reach.
            exact_match='"' in query,
        )
    except Exception as e:
        return f"Web search error: {e}"

    results = payload.get("results", [])
    ctx.deps.sources_seen.extend(_sources(results, query=query, tool="search_web"))
    _remember(ctx, results, body_key="content")

    if not results:
        return f"No web results for: {query}"

    return _json(
        query=query,
        results=[{k: r[k] for k in _RESULT_FIELDS if k in r} for r in results],
    )


async def extract_pages(
    ctx: RunContext[ForecastDeps], urls: list[str], query: str | None = None
) -> str:
    """Read the full text of pages you have already found.

    Use this when a search snippet is not enough to judge a claim — a report's actual
    numbers, the wording of a resolution criterion, what a filing really says. Pass the URLs
    a search returned, not URLs you guessed.

    args:
        urls: List of urls to extract data from.
        query: User intent for reranking extracted content chunks. When provided, chunks are reranked based on relevance to this query.
    """
    wanted = [u for u in urls if u][:MAX_EXTRACT_URLS]
    if not wanted:
        return "No URLs given to extract."

    client = _client()
    if client is None:
        return _unavailable("Page extraction", ", ".join(wanted[:2]))

    try:
        payload = await client.extract(
            wanted,
            extract_depth="basic",
            format="markdown",
            timeout=_SLOW_TIMEOUT,
            include_favicon=True,
            include_usage=True,
            query=query,
        )
    except Exception as e:
        return f"Page extraction error: {e}"

    results = payload.get("results") or []
    ctx.deps.sources_seen.extend(
        _sources(results, query=", ".join(wanted), tool="extract_pages")
    )
    _remember(ctx, results, body_key="raw_content")
    if not results:
        return f"Could not extract any of: {', '.join(wanted)}"

    return _json(
        pages=[
            {
                "title": r.get("title") or "",
                "url": r.get("url", ""),
                "text": (r.get("raw_content") or "")[:_PAGE_CHARS],
            }
            for r in results
        ],
        could_not_read=[
            f.get("url", "") for f in (payload.get("failed_results") or [])
        ],
    )


async def crawl_site(
    ctx: RunContext[ForecastDeps], url: str, instructions: str = ""
) -> str:
    """Read several pages of one site, following its links.

    Use this for a source that spreads what you need across pages — a statistics agency's
    release series, a regulator's decision archive. `instructions` steers which links are
    followed, in plain words: "monetary policy decisions", say. Prefer `extract_pages` when
    you already know the page you want.
    """
    client = _client()
    if client is None:
        return _unavailable("Site crawl", url)

    try:
        payload = await client.crawl(
            url,
            max_depth=CRAWL_DEPTH,
            limit=MAX_CRAWL_PAGES,
            instructions=instructions or None,
            format="markdown",
            timeout=_SLOW_TIMEOUT,
            include_favicon=True,
            include_usage=True,
        )
    except Exception as e:
        return f"Site crawl error: {e}"

    results = payload.get("results") or []
    ctx.deps.sources_seen.extend(
        _sources(results, query=instructions or url, tool="crawl_site")
    )
    if not results:
        return f"Crawling {url} returned no pages."
    return _json(
        site=url,
        pages=[
            {
                "url": r.get("url", ""),
                "text": (r.get("raw_content") or "")[:_PAGE_CHARS],
            }
            for r in results
        ],
    )


async def map_site(
    ctx: RunContext[ForecastDeps], url: str, instructions: str | None = None
) -> str:
    """List the pages a site has, without reading any of them.

    Cheap reconnaissance: use it to find the right page before spending an `extract_pages`
    call, when a site's structure is not obvious from search results.

    args:
        url: the website to find all links in
        instructions: Natural language instructions for the crawler. Used to rank and prioritize the URLs. Use sparingly
    """
    client = _client()
    if client is None:
        return _unavailable("Site map", url)

    try:
        payload = await client.map(
            url,
            max_depth=CRAWL_DEPTH,
            limit=MAX_MAP_LINKS,
            timeout=_SLOW_TIMEOUT,
            include_usage=True,
            instructions=instructions,
        )
    except Exception as e:
        return f"Site map error: {e}"

    links = [link for link in (payload.get("results") or []) if isinstance(link, str)]
    if not links:
        return f"Mapping {url} returned no links."

    # No SourceRef here. Mapping lists what exists; it does not read a page, so nothing on
    # this list is evidence the agent has seen. Recording them would let `check_citations`
    # pass a URL the agent never read.
    return f"{len(links)} pages under {url}:\n" + "\n".join(f"- {u}" for u in links)


async def find_disconfirming_evidence(ctx: RunContext[ForecastDeps], claim: str) -> str:
    """Search specifically for evidence AGAINST a claim.

    Use this before settling on a probability, not after. Ordinary search tends to
    return material that supports however the claim was phrased; this runs several
    rewrites aimed at the opposite conclusion.
    """
    angles = [
        f"evidence against {claim}",
        f"why {claim} will not happen",
        f"{claim} criticism skepticism doubts",
    ]
    sections: list[str] = []
    for angle in angles:
        result = await search_web(ctx, angle)
        sections.append(f"### {angle}\n{result}")
    return "\n\n".join(sections)


# Helpers

_RESULT_FIELDS = ("title", "url", "content", "published_date", "score")
"""The fields of a Tavily search result an agent is given.

`raw_content` is left out. It is the whole page rather than the matching excerpt, and
`extract_pages` is the tool for that, on the pages the agent chose to spend a call on.
"""


def _json(**fields) -> str:
    """A tool result as JSON.

    Tavily already answers with parsed dicts, so a tool passes them through rather than
    rendering text. Nothing to keep in step when a field is added or renamed.
    """
    return json.dumps(fields, ensure_ascii=False, default=str)


def _client() -> AsyncTavilyClient | None:
    """A client for the key that is set right now, or None when there is none.

    Built per call rather than cached, because `get_settings()` re-reads the environment on
    every call and that is what makes the runtime key panel work without a restart.
    """
    key = get_settings().tavily_api_key
    return AsyncTavilyClient(api_key=key) if key else None


def _remember(
    ctx: RunContext[ForecastDeps], results: list[dict], *, body_key: str
) -> None:
    """Keep the page text in the run's research store instead of discarding it.

    The text is already here and already paid for — the model reads it once and the
    context window then loses it. Storing it lets a later stage read it back for a tool
    call instead of another search.

    Failure is silent by design. A store that cannot be written is a lost convenience,
    and raising here would turn it into a lost search.
    """
    store = ctx.deps.store
    if store is None or not results:
        return

    docs = [
        ResearchDoc(
            url=r["url"],
            title=r.get("title") or "",
            body=(r.get(body_key) or "")[:_PAGE_CHARS],
        )
        for r in results
        if r.get("url")
    ]
    try:
        store.remember(docs)
    except Exception:
        pass


def _sources(results: list[dict], *, query: str, tool: str) -> list[SourceRef]:
    return [
        SourceRef(url=r["url"], title=r.get("title") or "", query=query, tool=tool)
        for r in results
        if r.get("url")
    ]


def _unavailable(tool: str, subject: str) -> str:
    return f"[{tool} unavailable — no TAVILY_API_KEY set. Asked about: {subject}]"
