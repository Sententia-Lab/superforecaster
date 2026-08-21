"""The research store as a tool: pages this run already fetched, ranked by BM25."""

from __future__ import annotations

from pydantic_ai import RunContext

from ..deps import ForecastDeps
from ..models import SourceRef

MAX_RESULTS = 5


async def search_research(
    ctx: RunContext[ForecastDeps], query: str, limit: int = MAX_RESULTS
) -> list[dict] | str:
    """Search the pages this run has already read. Search here before the web; an empty
    result means the run has not covered this ground yet."""
    store = ctx.deps.store
    if store is None:
        return "Research store unavailable: this run keeps no store."
    try:
        hits = store.find(query, limit=limit)
    except Exception as e:
        # A store that cannot be read is a lost convenience, not a lost cell.
        return f"Research store error: {e}"
    if not hits:
        return f"Nothing stored yet for: {query}. Search the web."
    ctx.deps.sources_seen.extend(
        SourceRef(url=h.url, title=h.title, query=query, tool="research_store")
        for h in hits
    )
    return [{"title": h.title, "url": h.url, "content": h.content} for h in hits]
