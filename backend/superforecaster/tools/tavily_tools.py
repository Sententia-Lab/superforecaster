"""The four Tavily endpoints, one tool each, over the `tavily-python` SDK.

| Tool | `AsyncTavilyClient` call | Answers |
|---|---|---|
| `search_web` | `.search()` | which pages discuss this |
| `extract_pages` | `.extract()` | what a page you already found says |
| `crawl_site` | `.crawl()` | what a site says across several pages |
| `map_site` | `.map()` | what pages a site has, without reading them |

**Only `search_web` runs in a backtest.** It takes `end_date`, and `_drop_leaked` re-checks
every result against `ctx.deps.forecast_date`. The other three take no date of any kind: they return
the page as it stands today, so in a backtest each is an uncontrolled leak. They refuse
instead of running, which keeps ADR 17 clamp 1 true for every path.

`search_web` takes a query and nothing else. Every other `POST /search` parameter is set
here, because `config.BUDGETS` counts calls rather than tokens and one call must not spend
a cell's whole budget: `max_results` is `MAX_RESULTS`, `chunks_per_source` is
`MAX_CHUNKS_PER_SOURCE`, and `timeout` is `_TIMEOUT`. `_search_kwargs` is applied last, so
no default above it can loosen the backtest clamp.

`extract_pages`, `crawl_site`, and `map_site` keep constant depth and breadth.

Every URL an agent sees is recorded on `ctx.deps.sources_seen`, so a run can be audited for
leakage rather than trusted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic_ai import RunContext
from tavily import AsyncTavilyClient

from ..config import get_settings
from ..deps import ForecastDeps
from ..models import SourceRef
from .dates import _as_utc, _parse_published

MAX_RESULTS = 5
MAX_CHUNKS_PER_SOURCE = 3
MAX_EXTRACT_URLS = 5
MAX_CRAWL_PAGES = 10
MAX_MAP_LINKS = 20
CRAWL_DEPTH = 5
_PAGE_CHARS = 2000
BACKTEST_WINDOW_DAYS = 3650

_TIMEOUT = 15.0
_SLOW_TIMEOUT = 60.0
"""Extract, crawl, and map fetch whole pages, so they are slower than a search. Both sit
inside `DEFAULT_AGENT_TIMEOUT_SECONDS` — a tool that outlives the agent kills the step."""

# Tools


async def search_web(
    ctx: RunContext[ForecastDeps],
    query: str,
    topic: Literal["general", "news", "finance"] = "general",
    exact_match: bool = False,
) -> str:
    """Search the web for current information and reporting.

    Returns the top results as text, or a message explaining why none are available.
    Missing results mean missing information, not an error — say so in your reasoning
    rather than treating it as a failure.

    Args:
        query: What to search for. Write it the way you would type it into a search engine. Wrap target phrases in quotes within your query (e.g. "John Smith" CEO Acme Corp) to get exact matches on those strings. Punctuation is typically ignored inside quotes.
        topic: The category of the search. News is useful for retrieving real-time updates, particularly about politics, sports, and major current events covered by mainstream media sources. General is for broader, more general-purpose searches that may include a wide range of sources.
        exact_match: when set to True, tool expects a quoted string in the query. If True but no quoted string, it will throw an error

    """
    forecast_date = ctx.deps.forecast_date
    client = _client()
    if client is None:
        return _unavailable("Web search", query)

    try:
        payload = await client.search(
            query,
            timeout=_TIMEOUT,
            search_depth="basic",
            max_results=MAX_RESULTS,
            chunks_per_source=MAX_CHUNKS_PER_SOURCE,
            include_favicon=True,
            include_usage=True,
            include_raw_content="markdown",
            topic=topic,
            exact_match=exact_match,
            **_search_kwargs(forecast_date),
        )
    except Exception as e:
        return f"Web search error: {e}"

    raw = payload.get("results", [])
    results, refs = _drop_leaked(raw, forecast_date, query)
    ctx.deps.sources_seen.extend(refs)

    if not results:
        if forecast_date is not None and raw:
            return (
                f"No web results for '{query}' published on or before "
                f"{forecast_date.date().isoformat()}. {len(raw)} newer or undated results were "
                "withheld — treat this as an absence of contemporaneous evidence."
            )
        return f"No web results for: {query}"

    header = (
        f"Results published on or before {forecast_date.date().isoformat()}:\n\n"
        if forecast_date is not None
        else ""
    )
    return header + "\n\n".join(
        f"- {r.get('title', 'Untitled')} ({r.get('url', '')})"
        + (f" [{r['published_date']}]" if r.get("published_date") else "")
        + f"\n  {r.get('content', '')[:400]}"
        for r in results
    )


async def extract_pages(
    ctx: RunContext[ForecastDeps], urls: list[str], query: str
) -> str:
    """Read the full text of pages you have already found.

    Use this when a search snippet is not enough to judge a claim — a report's actual
    numbers, the wording of a resolution criterion, what a filing really says. Pass the URLs
    a search returned, not URLs you guessed.

    args:
        urls: List of urls to extract data from.
        query: User intent for reranking extracted content chunks. When provided, chunks are reranked based on relevance to this query.
    """
    if ctx.deps.forecast_date is not None:
        return _not_in_a_backtest("extract_pages", ctx.deps.forecast_date)
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
    if not results:
        return f"Could not extract any of: {', '.join(wanted)}"

    body = "\n\n".join(
        f"## {r.get('title') or r.get('url', '')}\n{r.get('url', '')}\n\n"
        f"{(r.get('raw_content') or '')[:_PAGE_CHARS]}"
        for r in results
    )
    failed = [f.get("url", "") for f in (payload.get("failed_results") or [])]
    return body + (f"\n\nCould not read: {', '.join(failed)}" if failed else "")


async def crawl_site(
    ctx: RunContext[ForecastDeps], url: str, instructions: str = ""
) -> str:
    """Read several pages of one site, following its links.

    Use this for a source that spreads what you need across pages — a statistics agency's
    release series, a regulator's decision archive. `instructions` steers which links are
    followed, in plain words: "monetary policy decisions", say. Prefer `extract_pages` when
    you already know the page you want.
    """
    if ctx.deps.forecast_date is not None:
        return _not_in_a_backtest("crawl_site", ctx.deps.forecast_date)
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
    return f"Crawled {len(results)} pages of {url}:\n\n" + "\n\n".join(
        f"## {r.get('url', '')}\n{(r.get('raw_content') or '')[:_PAGE_CHARS]}"
        for r in results
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
    if ctx.deps.forecast_date is not None:
        return _not_in_a_backtest("map_site", ctx.deps.forecast_date)
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


def _client() -> AsyncTavilyClient | None:
    """A client for the key that is set right now, or None when there is none.

    Built per call rather than cached, because `get_settings()` re-reads the environment on
    every call and that is what makes the runtime key panel work without a restart.
    """
    key = get_settings().tavily_api_key
    return AsyncTavilyClient(api_key=key) if key else None


def _drop_leaked(
    results: list[dict], forecast_date: datetime | None, query: str = ""
) -> tuple[list[dict], list[SourceRef]]:
    """Second guard on top of Tavily's own `end_date` filter.

    Undated results are dropped when `forecast_date` is set. That is deliberate and it does
    cost recall: an article with no publication date cannot be shown to predate the
    question, and for a backtest "probably fine" is not good enough. Setting
    `topic="news"` means most results carry a date, so the loss is bounded.

    The guard is not theoretical. Asking Tavily for `end_date=2023-06-01` has been observed
    returning articles dated 2024 and 2026, so this is the only thing standing between a
    backtest and the answer.

    Returns the surviving results and a SourceRef for every result considered, including the
    dropped ones, so the audit trail shows what was filtered.
    """
    cutoff = _as_utc(forecast_date) if forecast_date is not None else None
    kept: list[dict] = []
    refs: list[SourceRef] = []
    for r in results:
        published = _parse_published(r.get("published_date"))
        refs.append(
            SourceRef(
                url=r.get("url", ""),
                title=r.get("title", "") or "",
                query=query,
                published_date=published,
                tool="search_web",
                forecast_date=cutoff,
            )
        )
        if cutoff is None or (published is not None and published <= cutoff):
            kept.append(r)
    return kept, refs


def _sources(results: list[dict], *, query: str, tool: str) -> list[SourceRef]:
    return [
        SourceRef(url=r["url"], title=r.get("title") or "", query=query, tool=tool)
        for r in results
        if r.get("url")
    ]


def _unavailable(tool: str, subject: str) -> str:
    return f"[{tool} unavailable — no TAVILY_API_KEY set. Asked about: {subject}]"


def _not_in_a_backtest(tool: str, forecast_date: datetime) -> str:
    """Why extract, crawl, and map do not run against a past date.

    Phrased for the agent rather than for a log: it needs to know the door is shut on
    purpose and that searching is still open, or it will retry the same call.
    """
    return (
        f"[{tool} is disabled for this run. You are forecasting as of "
        f"{forecast_date.date().isoformat()}, and this tool returns pages as they stand today, "
        "which would show you the future. Use search_web, which is filtered to that date.]"
    )


def _search_kwargs(forecast_date: datetime | None) -> dict:
    if forecast_date is None:
        return {}
    return {
        "start_date": (forecast_date - timedelta(days=BACKTEST_WINDOW_DAYS))
        .date()
        .isoformat(),
        "end_date": forecast_date.date().isoformat(),
    }
