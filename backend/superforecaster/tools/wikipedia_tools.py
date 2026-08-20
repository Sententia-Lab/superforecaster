"""Wikipedia lookup, clamped to a point in time.

With `ctx.deps.forecast_date` set this fetches the newest revision at or before that timestamp
rather than the current article, so the agent reads the page as it stood on the day the
question was asked.

This is the only date clamp left. The Tavily one was removed once measurement showed its
filter did nothing on a `general` search (ADR 17); the revisions API here is checked by
the response itself, which carries the revision timestamp.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from pydantic_ai import RunContext

from ..config import get_settings
from ..deps import ForecastDeps
from ..models import SourceRef
from .dates import _as_utc, _parse_published

WIKIPEDIA_URL = "https://en.wikipedia.org/w/api.php"

USER_AGENT = "superforecaster/0.3.0 (https://github.com/Sententia-Lab/superforecaster)"
"""Wikimedia's User-Agent policy asks for a name and a contact URL, and refuses anything
that looks like a default client. Anonymous access still needs this, key or no key."""

_TIMEOUT = 15.0


# ---------- request builders (pure — tested without a network call) ----------


def _wikipedia_search_params(topic: str, limit: int = 3) -> dict:
    return {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": topic,
        "srlimit": limit,
    }


def _wikipedia_params(title: str, forecast_date: datetime | None) -> dict:
    """Build the Wikipedia content request for one article.

    With `forecast_date` set we ask for the newest revision at or before that timestamp
    rather than the current article, so the agent reads the page as it stood on the
    day the question was asked.
    """
    if forecast_date is None:
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
        "rvstart": _as_utc(forecast_date).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rvdir": "older",
        "rvlimit": 1,
        "rvprop": "content|timestamp",
        "rvslots": "main",
    }


def _wikipedia_headers() -> dict[str, str]:
    """The `User-Agent` Wikimedia requires, plus a bearer token when one is configured.

    The API key is optional and only raises the rate limit. The `User-Agent` is not
    optional — Wikimedia answers a default client agent with 403, so without this header
    every call fails and the agent spends a tool call learning nothing.
    """
    headers = {"User-Agent": USER_AGENT}
    key = get_settings().wikipedia_api_key
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _extract_page_text(
    page: dict, forecast_date: datetime | None
) -> tuple[str, datetime | None]:
    """Pull article text out of either response shape.

    Current articles come back under `extract`; historical revisions come back under
    `revisions[0].slots.main['*']` with a timestamp.
    """
    if forecast_date is None:
        return page.get("extract", ""), None

    revisions = page.get("revisions") or []
    if not revisions:
        return "", None
    revision = revisions[0]
    slot = (revision.get("slots") or {}).get("main") or {}
    content = slot.get("*") or revision.get("*") or ""
    return content, _parse_published(revision.get("timestamp"))


# ---------- tools ----------


async def search_wikipedia(ctx: RunContext[ForecastDeps], topic: str) -> str:
    """Look up background context, reference classes, and historical base rates.

    The API key is optional — set it to raise the rate limit, or leave it unset.
    """
    forecast_date = ctx.deps.forecast_date
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
                params=_wikipedia_params(top_title, forecast_date),
                headers=headers,
            )
            content_resp.raise_for_status()
            pages = content_resp.json().get("query", {}).get("pages", {})
    except httpx.HTTPError as e:
        return f"Wikipedia error: {e}"

    if not pages:
        return f"Wikipedia article '{top_title}' had no content"

    page = next(iter(pages.values()))
    text, revision_date = _extract_page_text(page, forecast_date)
    if not text:
        if forecast_date is not None:
            return (
                f"Wikipedia article '{top_title}' has no revision from on or before "
                f"{forecast_date.date().isoformat()} — the article did not exist yet."
            )
        return f"Wikipedia article '{top_title}' had no content"

    ctx.deps.sources_seen.append(
        SourceRef(
            url=f"https://en.wikipedia.org/wiki/{top_title.replace(' ', '_')}",
            title=top_title,
            query=topic,
            published_date=revision_date,
            tool="search_wikipedia",
            forecast_date=_as_utc(forecast_date) if forecast_date else None,
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
