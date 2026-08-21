"""The research store, as one tool the agent can read.

`search_web` and `extract_pages` write every page they fetch into the store. This reads
it back, ranked by BM25, scoped to the run that fetched it. Nothing else reads it: an
agent decides for itself whether what the run already found answers the question, rather
than a cache deciding on its behalf.

The store fills as the run goes, so this answers nothing during the first research stage
and grows useful after it. `agents.withdraw_tools` stops offering it while it is empty.
"""

from __future__ import annotations

import json

from pydantic_ai import RunContext

from ..deps import ForecastDeps
from ..models import SourceRef

MAX_RESULTS = 5


async def search_research(
    ctx: RunContext[ForecastDeps], query: str, limit: int = MAX_RESULTS
) -> str:
    """Search the research this forecast has already read, without going to the web.

    Search here before you search the web. A thin result means the run has not covered
    this ground yet, so search the web next. It does not mean the topic is unresearchable.

    Args:
        query: What to look for. Write it the way you would type it into a search engine. Every word counts towards the ranking; a page does not have to contain all of them.
        limit: How many pages to return, most relevant first.

    """
    store = ctx.deps.store
    if store is None:
        return "[Research store unavailable — this run keeps no store.]"

    hits = store.find(query, limit=limit)
    if not hits:
        return (
            f"[Nothing stored yet for: {query}. This run has not read a page matching "
            "it. Search the web.]"
        )

    ctx.deps.sources_seen.extend(
        SourceRef(url=h.url, title=h.title, query=query, tool="research_store")
        for h in hits
    )

    return _json(
        source="research_store",
        note="Read during an earlier step of this run. No new search was performed.",
        results=[h.model_dump() for h in hits],
    )


def _json(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False, default=str)
