"""The four Tavily endpoints, one tool each, over the `tavily-python` SDK.

| Tool | `AsyncTavilyClient` call | Answers |
|---|---|---|
| `search_web` | `.search()` | which pages discuss this |
| `extract_pages` | `.extract()` | what a page you already found says |
| `crawl_site` | `.crawl()` | what a site says across several pages |
| `map_site` | `.map()` | what pages a site has, without reading them |

**Only `search_web` runs in a backtest.** It takes `end_date`, and `_drop_leaked` re-checks
every result against `ctx.deps.as_of`. The other three take no date of any kind: they return
the page as it stands today, so in a backtest each is an uncontrolled leak. They refuse
instead of running, which keeps ADR 17 clamp 1 true for every path.

`search_web` takes the whole `POST /search` parameter list, so a model can pick its own
depth, domains, and time window. Three things stay ours, because `config.BUDGETS` counts
calls rather than tokens and one call must not spend a cell's whole budget: `max_results`
is clamped to `MAX_RESULTS`, `chunks_per_source` to `MAX_CHUNKS_PER_SOURCE`, and `timeout`
is not in the signature at all. `_search_kwargs` is applied after the model's arguments, so
a `topic`, `start_date`, or `end_date` the model passed cannot loosen the backtest clamp.

`extract_pages`, `crawl_site`, and `map_site` keep constant depth and breadth.

Every URL an agent sees is recorded on `ctx.deps.sources_seen`, so a run can be audited for
leakage rather than trusted.
"""

from __future__ import annotations

from collections.abc import Sequence
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
CRAWL_DEPTH = 1
_PAGE_CHARS = 2000
"""Ceilings the model cannot raise, because they are not arguments it can pass."""

_TIMEOUT = 15.0
_SLOW_TIMEOUT = 60.0
"""Extract, crawl, and map fetch whole pages, so they are slower than a search. Both sit
inside `DEFAULT_AGENT_TIMEOUT_SECONDS` — a tool that outlives the agent kills the step."""


def _in_range(value: int | None, low: int, high: int) -> int | None:
    """The number the model asked for, held inside a range this tool sets."""
    return None if value is None else min(max(value, low), high)


def _client() -> AsyncTavilyClient | None:
    """A client for the key that is set right now, or None when there is none.

    Built per call rather than cached, because `get_settings()` re-reads the environment on
    every call and that is what makes the runtime key panel work without a restart.
    """
    key = get_settings().tavily_api_key
    return AsyncTavilyClient(api_key=key) if key else None


BACKTEST_WINDOW_DAYS = 3650
"""How far before `as_of` a clamped search may look.

Ten years, because a reference class wants depth — the width is not what makes the clamp
work, `start_date` merely being present is. Verified from 2 to 10 years with identical
results.
"""


def _search_kwargs(as_of: datetime | None) -> dict:
    """The date clamp, as keyword arguments.

    Three things, and **all three are required**:

    - `topic="news"` because Tavily only returns `published_date` on news results, and
      without that field `_drop_leaked` has nothing to check.
    - `end_date`, the cutoff itself.
    - `start_date`, which looks redundant beside `end_date` and is not. **Tavily silently
      ignores `end_date` when it arrives alone.** Measured across three queries: with
      `end_date` only, 15 of 15 results were published after the cutoff; adding
      `start_date`, 0 of 15 were. No error, no warning — the filter just does not apply.

    Before this, `_drop_leaked` discarded every result Tavily returned, so a clamped run was
    not merely protected, it was starved: the agent got "no contemporaneous evidence" for
    every search and fell back to a pre-research guess.
    """
    if as_of is None:
        return {}
    return {
        "topic": "news",
        "start_date": (as_of - timedelta(days=BACKTEST_WINDOW_DAYS)).date().isoformat(),
        "end_date": as_of.date().isoformat(),
    }


def _drop_leaked(
    results: list[dict], as_of: datetime | None, query: str = ""
) -> tuple[list[dict], list[SourceRef]]:
    """Second guard on top of Tavily's own `end_date` filter.

    Undated results are dropped when `as_of` is set. That is deliberate and it does
    cost recall: an article with no publication date cannot be shown to predate the
    question, and for a backtest "probably fine" is not good enough. Setting
    `topic="news"` means most results carry a date, so the loss is bounded.

    The guard is not theoretical. Asking Tavily for `end_date=2023-06-01` has been observed
    returning articles dated 2024 and 2026, so this is the only thing standing between a
    backtest and the answer.

    Returns the surviving results and a SourceRef for every result considered, including the
    dropped ones, so the audit trail shows what was filtered.
    """
    cutoff = _as_utc(as_of) if as_of is not None else None
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
                as_of=cutoff,
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


def _not_in_a_backtest(tool: str, as_of: datetime) -> str:
    """Why extract, crawl, and map do not run against a past date.

    Phrased for the agent rather than for a log: it needs to know the door is shut on
    purpose and that searching is still open, or it will retry the same call.
    """
    return (
        f"[{tool} is disabled for this run. You are forecasting as of "
        f"{as_of.date().isoformat()}, and this tool returns pages as they stand today, "
        "which would show you the future. Use search_web, which is filtered to that date.]"
    )


async def search_web(
    ctx: RunContext[ForecastDeps],
    query: str,
    search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] | None = None,
    chunks_per_source: int | None = None,
    topic: Literal["general", "news", "finance"] | None = None,
    time_range: Literal["day", "week", "month", "year"] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    max_results: int | None = None,
    include_domains: Sequence[str] | None = None,
    exclude_domains: Sequence[str] | None = None,
    country: str | None = None,
    include_answer: bool | Literal["basic", "advanced"] | None = None,
    include_raw_content: bool | Literal["markdown", "text"] | None = None,
    include_images: bool | None = None,
    include_image_descriptions: bool | None = None,
    include_favicon: bool | None = None,
    include_usage: bool | None = None,
    auto_parameters: bool | None = None,
    exact_match: bool | None = None,
) -> str:
    """Search the web for current information and reporting.

    Returns the top results as text, or a message explaining why none are available.
    Missing results mean missing information, not an error — say so in your reasoning
    rather than treating it as a failure.

    Leave an argument out and Tavily uses its own default. Set only what the question needs.

    Args:
        query: What to search for. Write it the way you would type it into a search engine.
        search_depth: How hard one query works, against how long it takes. `basic` is the
            default and suits most questions. `advanced` gives the highest relevance and
            costs two API credits instead of one. `fast` and `ultra-fast` trade relevance
            for speed. `ultra-fast` returns one summary per page instead of snippets.
        chunks_per_source: How many snippets to return per page, 1 to 3. Each snippet holds
            up to 500 characters of the page. Lower it to read more pages for the same
            number of tokens. `ultra-fast` ignores it.
        topic: The kind of search. `news` covers politics, sport, and current events from
            mainstream media. `general` is broader. `finance` covers markets and filings.
            Only `news` results carry a publication date.
        time_range: How far back to look from today, by publication or update date.
        start_date: Return only results published on or after this date, as YYYY-MM-DD.
        end_date: Return only results published on or before this date, as YYYY-MM-DD.
        days: How many days back to look. An alternative to `time_range`.
        max_results: How many results to return. Anything above 5 is lowered to 5.
        include_domains: Search these domains only. Use it when you already know the
            authority, such as "ons.gov.uk" for UK statistics. Maximum 300.
        exclude_domains: Skip these domains. Maximum 150.
        country: Prefer results from one country, as its lowercase English name, such as
            "united kingdom". Tavily accepts it only when `topic` is "general".
        include_answer: Add a generated answer above the results. `basic` is short,
            `advanced` is longer. Read the sources yourself before you rely on it.
        include_raw_content: Add the full text of every result, as "markdown" or "text".
            Expensive, and it slows the call down. Prefer `extract_pages` for the few pages
            you actually need.
        include_images: Add image URLs to the response.
        include_image_descriptions: Describe each image. Needs `include_images`.
        include_favicon: Add the favicon URL of each result.
        include_usage: Add how many API credits the call spent.
        auto_parameters: Let Tavily set the parameters from the query itself. It may raise
            `search_depth` to "advanced", which costs two credits. Set `search_depth` to
            "basic" yourself to avoid that. `max_results`, `include_answer`, and
            `include_raw_content` always stay as you set them.
        exact_match: Return only results that contain the quoted phrases in `query`. Put
            the phrase in double quotes inside the query itself.
    """
    as_of = ctx.deps.as_of
    client = _client()
    if client is None:
        return _unavailable("Web search", query)

    options = {
        "search_depth": search_depth,
        "chunks_per_source": _in_range(chunks_per_source, 1, MAX_CHUNKS_PER_SOURCE),
        "topic": topic,
        "time_range": time_range,
        "start_date": start_date,
        "end_date": end_date,
        "include_domains": include_domains,
        "exclude_domains": exclude_domains,
        "country": country,
        "include_answer": include_answer,
        "include_raw_content": include_raw_content,
        "include_images": include_images,
        "include_image_descriptions": include_image_descriptions,
        "include_favicon": include_favicon,
        "include_usage": include_usage,
        "auto_parameters": auto_parameters,
        "exact_match": exact_match,
    }
    options = {name: value for name, value in options.items() if value is not None}
    options.setdefault("search_depth", "basic")
    # Two ceilings the model cannot raise, because neither is read from its arguments.
    options["max_results"] = _in_range(max_results, 1, MAX_RESULTS) or MAX_RESULTS
    # Applied last, so a topic or date the model passed cannot loosen the backtest clamp.
    options.update(_search_kwargs(as_of))

    try:
        payload = await client.search(query, timeout=_TIMEOUT, **options)
    except Exception as e:
        return f"Web search error: {e}"

    raw = payload.get("results", [])
    results, refs = _drop_leaked(raw, as_of, query)
    ctx.deps.sources_seen.extend(refs)

    if not results:
        if as_of is not None and raw:
            return (
                f"No web results for '{query}' published on or before "
                f"{as_of.date().isoformat()}. {len(raw)} newer or undated results were "
                "withheld — treat this as an absence of contemporaneous evidence."
            )
        return f"No web results for: {query}"

    header = (
        f"Results published on or before {as_of.date().isoformat()}:\n\n"
        if as_of is not None
        else ""
    )
    return header + "\n\n".join(
        f"- {r.get('title', 'Untitled')} ({r.get('url', '')})"
        + (f" [{r['published_date']}]" if r.get("published_date") else "")
        + f"\n  {r.get('content', '')[:400]}"
        for r in results
    )


async def extract_pages(ctx: RunContext[ForecastDeps], urls: list[str]) -> str:
    """Read the full text of pages you have already found.

    Use this when a search snippet is not enough to judge a claim — a report's actual
    numbers, the wording of a resolution criterion, what a filing really says. Pass the URLs
    a search returned, not URLs you guessed.
    """
    if ctx.deps.as_of is not None:
        return _not_in_a_backtest("extract_pages", ctx.deps.as_of)
    wanted = [u for u in urls if u][:MAX_EXTRACT_URLS]
    if not wanted:
        return "No URLs given to extract."

    client = _client()
    if client is None:
        return _unavailable("Page extraction", ", ".join(wanted[:2]))

    try:
        payload = await client.extract(
            wanted, extract_depth="basic", format="markdown", timeout=_SLOW_TIMEOUT
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
    if ctx.deps.as_of is not None:
        return _not_in_a_backtest("crawl_site", ctx.deps.as_of)
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


async def map_site(ctx: RunContext[ForecastDeps], url: str) -> str:
    """List the pages a site has, without reading any of them.

    Cheap reconnaissance: use it to find the right page before spending an `extract_pages`
    call, when a site's structure is not obvious from search results.
    """
    if ctx.deps.as_of is not None:
        return _not_in_a_backtest("map_site", ctx.deps.as_of)
    client = _client()
    if client is None:
        return _unavailable("Site map", url)

    try:
        payload = await client.map(
            url, max_depth=CRAWL_DEPTH, limit=MAX_MAP_LINKS, timeout=_SLOW_TIMEOUT
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
    # Three searches inside one tool call cost one call, because `search_web` is invoked
    # here as a plain function and only the outer call reaches the toolset. The model made
    # one decision, so it is charged for one.
    sections: list[str] = []
    for angle in angles:
        result = await search_web(ctx, angle)
        sections.append(f"### {angle}\n{result}")
    return "\n\n".join(sections)
