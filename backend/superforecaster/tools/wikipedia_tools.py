"""Wikipedia lookup: the top search hit's introduction."""

from __future__ import annotations

import httpx
from pydantic_ai import RunContext

from ..config import get_settings
from ..deps import ForecastDeps
from ..models import SourceRef

WIKIPEDIA_URL = "https://en.wikipedia.org/w/api.php"

USER_AGENT = "superforecaster/0.3.0 (https://github.com/Sententia-Lab/superforecaster)"
"""Wikimedia refuses a default client User-Agent, key or no key."""

_TIMEOUT = 15.0
_INTRO_CHARS = 1500


def _wikipedia_search_params(topic: str, limit: int = 3) -> dict:
    return {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": topic,
        "srlimit": limit,
    }


def _wikipedia_params(title: str) -> dict:
    return {
        "action": "query",
        "format": "json",
        "titles": title,
        "prop": "extracts",
        "explaintext": True,
        "exintro": True,
    }


def _wikipedia_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    key = get_settings().wikipedia_api_key
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def search_wikipedia(ctx: RunContext[ForecastDeps], topic: str) -> str:
    """Look up background, reference classes, and historical base rates on Wikipedia."""
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
                WIKIPEDIA_URL, params=_wikipedia_params(top_title), headers=headers
            )
            content_resp.raise_for_status()
            pages = content_resp.json().get("query", {}).get("pages", {})
    except httpx.HTTPError as e:
        return f"Wikipedia error: {e}"

    page = next(iter(pages.values()), {})
    text = page.get("extract", "")
    if not text:
        return f"Wikipedia article '{top_title}' had no content"

    ctx.deps.sources_seen.append(
        SourceRef(
            url=f"https://en.wikipedia.org/wiki/{top_title.replace(' ', '_')}",
            title=top_title,
            query=topic,
            tool="search_wikipedia",
        )
    )
    related = ", ".join(h["title"] for h in hits[:3])
    return f"Wikipedia: {top_title}\n\n{text[:_INTRO_CHARS]}\n\nRelated articles: {related}"
