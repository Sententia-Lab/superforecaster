"""The tools an agent may call, one module per upstream.

    tavily_tools     search_web, extract_pages, crawl_site, map_site,
                     find_disconfirming_evidence
    wikipedia_tools  search_wikipedia
    dates            timestamp parsing both of them need

The Tavily tools return the web as it stands today, whatever `ctx.deps.forecast_date`
says. They used to clamp to that date; ADR 17 records the measurements that removed it —
Tavily's `start_date`/`end_date` filter has no effect on a `general` search, and no topic
but `news` returns a `published_date` to check against.

`search_wikipedia` still honours `forecast_date`, because the MediaWiki revisions API
genuinely serves the article as it stood on a date.

Every URL an agent sees is recorded on `ctx.deps.sources_seen` so a run can be audited for
leakage rather than trusted.

Re-exported here so a caller writes `from ..tools import search_web` and does not have to
know which upstream serves it.
"""

from .tavily_tools import (
    crawl_site,
    extract_pages,
    find_disconfirming_evidence,
    map_site,
    search_web,
)
from .wikipedia_tools import search_wikipedia

__all__ = (
    "crawl_site",
    "extract_pages",
    "find_disconfirming_evidence",
    "map_site",
    "search_web",
    "search_wikipedia",
)
