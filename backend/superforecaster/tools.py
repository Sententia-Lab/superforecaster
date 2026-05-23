"""Shared search tools used by all three agents.

Each function is a plain async callable returning a string. Pydantic AI
agents register them via the `tools=[...]` argument at construction time.
"""

from __future__ import annotations

import httpx

from config import get_settings


async def search_web(query: str) -> str:
    """Search the web via Tavily for current information.

    Returns a concatenated string of the top results, or a graceful
    fallback message if `TAVILY_API_KEY` is not set. The agent treats
    the absence of search results as missing information, not as an error.
    """
    api_key = get_settings().tavily_api_key
    if not api_key:
        return f"[Web search unavailable — no TAVILY_API_KEY set. Query was: {query}]"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": 5,
                    "include_answer": False,
                    "search_depth": "basic",
                },
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            if not results:
                return f"No web results for: {query}"
            return "\n\n".join(
                f"- {r.get('title', 'Untitled')} ({r.get('url', '')})\n"
                f"  {r.get('content', '')[:400]}"
                for r in results
            )
    except httpx.HTTPError as e:
        return f"Web search error: {e}"


async def search_wikipedia(topic: str) -> str:
    """Search Wikipedia for background context, base rates, reference classes.

    Uses the public Wikipedia API. No key required.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            search_resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "list": "search",
                    "srsearch": topic,
                    "srlimit": 3,
                },
            )
            search_resp.raise_for_status()
            results = search_resp.json().get("query", {}).get("search", [])
            if not results:
                return f"No Wikipedia results for: {topic}"

            # Fetch the intro of the top result
            top_title = results[0]["title"]
            extract_resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "titles": top_title,
                    "prop": "extracts",
                    "explaintext": True,
                    "exintro": True,
                },
            )
            extract_resp.raise_for_status()
            pages = extract_resp.json().get("query", {}).get("pages", {})
            if not pages:
                return f"Wikipedia article '{top_title}' had no content"
            page = next(iter(pages.values()))
            extract = page.get("extract", "")
            related = ", ".join(r["title"] for r in results[:3])
            return (
                f"Wikipedia: {top_title}\n\n{extract[:1500]}\n\n"
                f"Related articles: {related}"
            )
    except httpx.HTTPError as e:
        return f"Wikipedia error: {e}"
