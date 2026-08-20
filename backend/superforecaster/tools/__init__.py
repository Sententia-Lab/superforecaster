"""The tools an agent may call, one module per upstream.

    tavily_tools     search_web, extract_pages, crawl_site, map_site,
                     find_disconfirming_evidence
    wikipedia_tools  search_wikipedia
    dates            timestamp parsing both of them need

Each tool takes `RunContext[ForecastDeps]` and reads `ctx.deps.forecast_date`. When it is set, the
tool must not return anything published after that date — this is clamp 1 of the two that
make backtesting against resolved questions honest. (Clamp 2, the model's training cutoff,
lives in `model_garden`.) `extract_pages`, `crawl_site`, and `map_site` cannot honour it —
no Tavily endpoint but search takes a date — so they refuse to run rather than leak.

When `forecast_date` is None the tools return current results with no filtering. That is the
production path.

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
