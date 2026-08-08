"""Shared search tools, clamped to a point in time.

Each tool takes `RunContext[ForecastDeps]` and reads `ctx.deps.as_of`. When it is
set, the tool must not return anything published after that date — this is clamp 1
of the two that make backtesting against resolved questions honest. (Clamp 2, the
model's training cutoff, lives in `model_garden`.)

When `as_of` is None the tools behave exactly as they did before: current results,
no filtering. That is the production path.

Every URL an agent sees is recorded on `ctx.deps.sources_seen` so a backtest run can
be audited for leakage rather than trusted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from config import get_settings
from pydantic_ai import RunContext

from .deps import ForecastDeps
from .models import SourceRef

TAVILY_URL = "https://api.tavily.com/search"
WIKIPEDIA_URL = "https://en.wikipedia.org/w/api.php"

_TIMEOUT = 15.0


# ---------- request builders (pure — tested without a network call) ----------


def _tavily_body(query: str, as_of: datetime | None, *, max_results: int = 5) -> dict:
    """Build the Tavily request body.

    When `as_of` is set we add `end_date` and switch `topic` to "news". The topic
    switch is not cosmetic: Tavily only returns `published_date` on news results, and
    without that field `_drop_leaked` has nothing to check.
    """
    body: dict = {
        "query": query,
        "max_results": max_results,
        "include_answer": False,
        "search_depth": "basic",
    }
    if as_of is not None:
        body["topic"] = "news"
        body["end_date"] = as_of.date().isoformat()
    return body


def _wikipedia_search_params(topic: str, limit: int = 3) -> dict:
    return {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": topic,
        "srlimit": limit,
    }


def _wikipedia_params(title: str, as_of: datetime | None) -> dict:
    """Build the Wikipedia content request for one article.

    With `as_of` set we ask for the newest revision at or before that timestamp
    rather than the current article, so the agent reads the page as it stood on the
    day the question was asked.
    """
    if as_of is None:
        return {
            "action": "query",
            "format": "json",
            "titles": title,
            "prop": "extracts",
            "explaintext": True,
            "exintro": True,
        }
    return {
        "action": "query",
        "format": "json",
        "titles": title,
        "prop": "revisions",
        "rvstart": _as_utc(as_of).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rvdir": "older",
        "rvlimit": 1,
        "rvprop": "content|timestamp",
        "rvslots": "main",
    }


# ---------- date handling ----------


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_published(raw: str | None) -> datetime | None:
    """Parse whatever Tavily put in `published_date`.

    It is ISO 8601 in most responses and RFC 2822 in some, so both are tried.
    Returns None when the value is missing or unparseable — the caller decides what
    an unknown date means.
    """
    if not raw:
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return _as_utc(parsedate_to_datetime(raw))
    except (TypeError, ValueError):
        return None


def _drop_leaked(
    results: list[dict], as_of: datetime | None, query: str = ""
) -> tuple[list[dict], list[SourceRef]]:
    """Second guard on top of Tavily's own `end_date` filter.

    Undated results are dropped when `as_of` is set. That is deliberate and it does
    cost recall: an article with no publication date cannot be shown to predate the
    question, and for a backtest "probably fine" is not good enough. Setting
    `topic="news"` means most results carry a date, so the loss is bounded.

    Returns the surviving results and a SourceRef for every result considered,
    including the dropped ones, so the audit trail shows what was filtered.
    """
    if as_of is None:
        refs = [
            SourceRef(
                url=r.get("url", ""),
                title=r.get("title", "") or "",
                query=query,
                published_date=_parse_published(r.get("published_date")),
                tool="search_web",
            )
            for r in results
        ]
        return results, refs

    cutoff = _as_utc(as_of)
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
        if published is not None and published <= cutoff:
            kept.append(r)
    return kept, refs


def _format_results(results: list[dict]) -> str:
    return "\n\n".join(
        f"- {r.get('title', 'Untitled')} ({r.get('url', '')})"
        + (f" [{r['published_date']}]" if r.get("published_date") else "")
        + f"\n  {r.get('content', '')[:400]}"
        for r in results
    )


# ---------- the search budget ----------


def _budget_notice(ctx: RunContext[ForecastDeps]) -> str:
    """Tell the agent how much search budget is left, appended to every tool result.

    This is the primary channel for the cline, and the reason is timing: it arrives at
    the exact moment the decision is made. The model just asked for a result and is about
    to read it; the last line of that result is the budget. There is no attention to
    compete for and nothing to skim past. It is also the only channel that can say "this
    is your last tool result", which is the sentence that matters most.

    Its two blind spots — nothing before the first request, silence after the last result
    while the model may still make several more — are covered by the dynamic instruction
    in `agents.attach_budget_pressure`.

    Increments at the *result*, not the call, so a search that errored still costs a
    call. That matches what pydantic-ai's own `UsageLimits` counts.

    Every line here names *searching* rather than tool calls in general. The structured
    answer is delivered as a tool call, so "stop calling tools" forbids the one call that
    would end the run — see `agents.attach_budget_pressure` for what that costs.

    Empty when nothing fanned out — the CLI, the update graph, an eval.
    """
    b = ctx.deps.budget
    if b is None:
        return ""

    b.used += 1
    if b.used < b.soft_depth:
        return f"\n\n[SEARCH BUDGET — {b.used} of {b.soft_depth} used.]"

    if b.left > 0:
        return (
            f"\n\n[SEARCH BUDGET SPENT — {b.used} of {b.soft_depth}. {b.left} call"
            f"{'s' if b.left != 1 else ''} remain before it is enforced. Stop searching. "
            "Write your answer from what you already have, and say in the source notes "
            "where the evidence is thin rather than looking for more. A thin answer "
            "graded honestly as thin is worth more than no answer.]"
        )

    b.exhausted = True
    return (
        "\n\n[SEARCH BUDGET EXHAUSTED — this is your last search result. Return your "
        "structured answer now, from what you have.]"
    )


# ---------- tools ----------


async def search_web(ctx: RunContext[ForecastDeps], query: str) -> str:
    """Search the web for current information and reporting.

    Returns the top results as text, or a message explaining why none are available.
    Missing results mean missing information, not an error — say so in your reasoning
    rather than treating it as a failure.
    """
    return await _search_web(ctx, query) + _budget_notice(ctx)


async def _search_web(ctx: RunContext[ForecastDeps], query: str) -> str:
    """The search itself, with no budget accounting.

    Split from the tool so `find_disconfirming_evidence` — which runs three of these
    inside one tool call — costs one call rather than three. The model made one decision;
    charging it three would make `SearchBudget.used` and `UsageLimits.tool_calls_limit`
    count different things.
    """
    as_of = ctx.deps.as_of
    api_key = get_settings().tavily_api_key
    if not api_key:
        return f"[Web search unavailable — no TAVILY_API_KEY set. Query was: {query}]"

    # api_key is merged in here rather than built into _tavily_body so the pure
    # request builder stays free of secrets and can be asserted on directly.
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                TAVILY_URL,
                json={**_tavily_body(query, as_of), "api_key": api_key},
            )
            response.raise_for_status()
            raw = response.json().get("results", [])
    except httpx.HTTPError as e:
        return f"Web search error: {e}"

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
    return header + _format_results(results)


def _wikipedia_headers() -> dict[str, str]:
    """A bearer token when one is configured, and no header otherwise.

    The API key is optional. Wikimedia serves this endpoint anonymously; an access token
    only raises the rate limit.
    """
    key = get_settings().wikipedia_api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


async def search_wikipedia(ctx: RunContext[ForecastDeps], topic: str) -> str:
    """Look up background context, reference classes, and historical base rates.

    The API key is optional — set it to raise the rate limit, or leave it unset.
    """
    return await _search_wikipedia(ctx, topic) + _budget_notice(ctx)


async def _search_wikipedia(ctx: RunContext[ForecastDeps], topic: str) -> str:
    """The lookup itself. Split from the tool so every return path — including the ones
    that found nothing — costs exactly one call and carries the budget line."""
    as_of = ctx.deps.as_of
    headers = _wikipedia_headers()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            search_resp = await client.get(
                WIKIPEDIA_URL, params=_wikipedia_search_params(topic), headers=headers
            )
            search_resp.raise_for_status()
            hits = search_resp.json().get("query", {}).get("search", [])
            if not hits:
                return f"No Wikipedia results for: {topic}"

            top_title = hits[0]["title"]
            content_resp = await client.get(
                WIKIPEDIA_URL,
                params=_wikipedia_params(top_title, as_of),
                headers=headers,
            )
            content_resp.raise_for_status()
            pages = content_resp.json().get("query", {}).get("pages", {})
    except httpx.HTTPError as e:
        return f"Wikipedia error: {e}"

    if not pages:
        return f"Wikipedia article '{top_title}' had no content"

    page = next(iter(pages.values()))
    text, revision_date = _extract_page_text(page, as_of)
    if not text:
        if as_of is not None:
            return (
                f"Wikipedia article '{top_title}' has no revision from on or before "
                f"{as_of.date().isoformat()} — the article did not exist yet."
            )
        return f"Wikipedia article '{top_title}' had no content"

    ctx.deps.sources_seen.append(
        SourceRef(
            url=f"https://en.wikipedia.org/wiki/{top_title.replace(' ', '_')}",
            title=top_title,
            query=topic,
            published_date=revision_date,
            tool="search_wikipedia",
            as_of=_as_utc(as_of) if as_of else None,
        )
    )

    related = ", ".join(h["title"] for h in hits[:3])
    stamp = (
        f" (revision of {revision_date.date().isoformat()})"
        if revision_date is not None
        else ""
    )
    return (
        f"Wikipedia: {top_title}{stamp}\n\n{text[:1500]}\n\nRelated articles: {related}"
    )


def _extract_page_text(
    page: dict, as_of: datetime | None
) -> tuple[str, datetime | None]:
    """Pull article text out of either response shape.

    Current articles come back under `extract`; historical revisions come back under
    `revisions[0].slots.main['*']` with a timestamp.
    """
    if as_of is None:
        return page.get("extract", ""), None

    revisions = page.get("revisions") or []
    if not revisions:
        return "", None
    revision = revisions[0]
    slot = (revision.get("slots") or {}).get("main") or {}
    content = slot.get("*") or revision.get("*") or ""
    return content, _parse_published(revision.get("timestamp"))


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
        result = await _search_web(ctx, angle)
        sections.append(f"### {angle}\n{result}")
    return "\n\n".join(sections) + _budget_notice(ctx)
