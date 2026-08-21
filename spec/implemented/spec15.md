# Spec 15 — The research store

## What problem this solves

A run fetches a page, the model reads it once, and the text is gone. Only
`SourceRef(url, title, query, tool)` survives on `ctx.deps.sources_seen` — enough to audit
a citation, not enough to read the page again. A later stage that wants what an earlier one
already fetched has to fetch it again and pay a tool call for it.

The store keeps the page text, scoped to the run that read it, and gives the research
agents one tool that reads it back.

## Where it sits

```
tavily_tools.search_web    ──► ctx.deps.store.remember(docs) ──► research_docs
tavily_tools.extract_pages ──►                                        │
                                                           research_index (FTS5)
                                                                      ▲
research_store_tools.search_research ──► ctx.deps.store.find(query) ───┘

api/forecasts.DELETE /{id} ──► db.delete_forecast ──► research.delete_research
```

`ctx.deps.store` is a `ResearchStore` protocol, not an import. `superforecaster/` may not
import `app` (ADR 73), and the store is SQLite — so the library declares three methods and
`app` implements them, the same way `app` already supplies the `emit` sink.

```python
# superforecaster/deps.py
class ResearchStore(Protocol):
    def remember(self, docs: list[ResearchDoc]) -> int: ...
    def find(self, query: str, limit: int = 5) -> list[ResearchHit]: ...
    def is_empty(self) -> bool: ...

# app/research.py
@dataclass(frozen=True)
class SqliteResearchStore:
    research_id: str
```

The store is bound to its `research_id` at construction, so no tool ever handles one and no
run can name another run's store.

## Schema — `SCHEMA_VERSION` 4 → 5

```sql
CREATE TABLE research_docs (
    research_id TEXT NOT NULL, url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (research_id, url)
);

CREATE VIRTUAL TABLE research_index USING fts5(
    title, body,
    content='research_docs', content_rowid='rowid', tokenize='porter unicode61'
);
-- research_ai / research_ad / research_au keep the index in step
```

Two tables because FTS5 has no primary key and no usable index on an `UNINDEXED` column.
Measured: `INSERT OR REPLACE` into an FTS5 table appends a duplicate.

`(research_id, url)` — one run, one page — is the whole identity of a row, so it is the
primary key rather than a `UNIQUE` beside a surrogate one. Nothing ever asks for a page by
any other name. Its index answers `WHERE research_id = ?` on the leading column, so the
scope lookup and the delete need no second index. The FTS5 join uses `rowid`, which SQLite
maintains whatever the primary key is.

Four columns, because four is what anything reads. Provenance — which query found a page,
which tool fetched it, when — is `SourceRef`'s job and is read there; a second copy here
was read by nothing. See ADR 80.

`forecasts.research_id` and `gated_runs.research_id` are added by `MIGRATIONS[5]`.
`research_id` is **not** a foreign key: pages are written while the run is in flight, before
any `forecasts` row exists.

## Data lineage

### Write — `search_web` keeps what it fetched

```
search_web(ctx, query="steel tariff exclusions 2026", topic="general")
  -> AsyncTavilyClient.search(...)
  -> ctx.deps.sources_seen.extend(_sources(...))                    [unchanged]
  -> _remember(ctx, results, body_key="content")
       -> ctx.deps.store.remember([ResearchDoc, ...])               [writes: research_docs]
  -> _json(results=[...])  -> str to the model                      [unchanged]
```

One row:

```json
{"research_id": "a418c9bc…",
 "url": "https://ustr.gov/exclusions",
 "title": "Steel tariff exclusions",
 "body": "The exclusion process for steel was renewed in 2026."}
```

`extract_pages` writes the same way, reading `raw_content` instead of `content`. Failure is
silent: a store that cannot be written is a lost convenience, and raising would turn it into
a lost search.

### Read — an inside-view cell, one stage later

```
search_research(ctx, query="US-China steel tariff negotiations 2026", limit=5)
  -> ctx.deps.store.find(query, limit=5)
       -> _match_expression(query)
          -> '"US" OR "China" OR "steel" OR "tariff" OR "negotiations" OR "2026"'
       -> SELECT d.url, d.title, d.body, -bm25(research_index, 5.0, 0.5) AS score
          FROM research_index JOIN research_docs d ON d.rowid = research_index.rowid
          WHERE research_index MATCH ? AND d.research_id = ?
          ORDER BY score DESC LIMIT ?
  -> ctx.deps.sources_seen.extend(SourceRef(..., tool="research_store"))
  -> _json(...)  -> str to the model
```

```json
{"source": "research_store",
 "note": "Read during an earlier step of this run. No new search was performed.",
 "results": [
   {"rank": 1, "score": 1.1737, "url": "https://reuters.com/talks",
    "title": "China trade talks",
    "content": "Beijing and Washington resumed tariff negotiations on steel."},
   {"rank": 2, "score": 0.5915, "url": "https://ustr.gov/exclusions",
    "title": "Steel tariff exclusions",
    "content": "The US exclusion process for steel tariffs was renewed in 2026."}]}
```

`OR`, not `AND`. No page contains every word of that query; an all-tokens rule returns 0 and
`OR` returns 3, ranked. Ranking filters, matching does not.

Escaping is not optional — FTS5 reads `MATCH` as syntax and the agent writes prose:

| Agent query | Raw `MATCH` | Escaped |
|---|---|---|
| `US-China tariffs` | `no such column: China` | 1 hit |
| `Trump: what next?` | `no such column: Trump` | 0 hits, no error |
| `AND` | `fts5: syntax error near "AND"` | 0 hits, no error |

### Delete — one transaction

```
DELETE /forecasts/{id}
  -> db.delete_forecast(forecast_id)
       SELECT research_id FROM forecasts WHERE id = ?     -> 404 if absent
       research.delete_research(research_id, conn)        [deletes: research_docs]
                                                          [research_index follows by trigger]
       DELETE FROM forecasts WHERE id = ?                 [cascades: forecast_updates]
                                                          [gated_runs.forecast_id -> NULL]
  -> 204
```

The mirror of `DELETE /runs/{id}`: that one keeps the forecast, this one keeps the run.

### Where `research_id` comes from

```
gated run:   machine.start_run -> db.start_gated_run   mints uuid4 -> gated_runs.research_id
             machine.execute_step -> SqliteResearchStore(run["research_id"]) -> ForecastDeps
             synthesis -> db.complete_gated_run copies it -> forecasts.research_id

ungated:     api/forecasts.create_forecast -> research.new_store()
             -> run_all(..., store=store) -> db.save_forecast(research_id=store.research_id)

CLI:         app/cli.py, same two lines as the ungated path
```

A backlog run has no `research_id` — research has not started. A run that was already active
when this shipped keeps NULL and simply keeps no store.

## Files

| File | Change |
|---|---|
| `app/research.py` | **new.** `index_documents`, `search_research`, `has_documents`, `delete_research`, `_match_expression`, `SqliteResearchStore`, `new_store` |
| `app/db.py` | `SCHEMA_VERSION = 5`, `MIGRATIONS[5]`, the two tables + three triggers, `save_forecast(research_id=)`, `delete_forecast`, `start_gated_run` mints, `complete_gated_run` copies |
| `superforecaster/deps.py` | `ResearchStore` protocol, `ForecastDeps.store` |
| `superforecaster/models.py` | `ResearchDoc`, `ResearchHit` |
| `superforecaster/tools/research_store_tools.py` | **new.** `search_research` |
| `superforecaster/tools/tavily_tools.py` | `_remember`, called by `search_web` and `extract_pages` |
| `superforecaster/agents/__init__.py` | `withdraw_tools` drops `search_research` while the store is empty; `SEARCH_RESERVE` → `CALL_RESERVE` |
| `superforecaster/config.py` | `base_rate_cell`/`inside_view` 8→10 calls, 11→13 iterations; `critic` 3→4, 6→7 |
| `superforecaster/stages.py` | `run_all(..., store=None)` |
| `agents/outside_view.py`, `inside_view.py`, `critic.py` | the tool, and the instruction to read the store first |
| `api/forecasts.py` | `DELETE /forecasts/{id}`; `POST /forecasts` builds a store |
| `app/cli.py`, `app/machine.py` | build a store |
| `pyproject.toml` | `bm25s` removed |

## Why the agent reads it through a tool, not a cache

`search_web` could have consulted the store and returned local results without calling
Tavily. Rejected: the agent is the one that knows whether what the run already found answers
*its* question, and a cache decides that on its behalf with a similarity threshold nobody
has measured.

The cost of that choice is that the prompt is the whole mechanism. ADR 77 measured
`extract_pages`, `crawl_site`, and `map_site` at zero calls each. So `outside_view`'s
numbered search ladder gains a rung 0, and `inside_view` and `critic` each gain a line.

A store read is an ordinary tool call — no counter, no exemption. `withdraw_tools` subtracts
`ctx.usage.tool_calls`, which this codebase cannot decrement. The budgets were raised
instead. `tokens` and `cost_usd` were not, so a cell can now reach its token ceiling before
its tool ceiling.

Full reasoning, including the FTS5-vs-`bm25s` measurements and the ADR 62 tension in the
budget bands, is **ADR 80**.

## Tests

| File | Covers |
|---|---|
| `tests/test_research_store.py` | idempotent init, v4→v5 migration, ranking, partial-token match, prose queries never raising, scope isolation, same URL in two runs, re-index, delete, no orphan index rows |
| `tests/test_research_tool.py` | `search_web` stores what it fetched, no store means no write, the tool reads back, a read is recorded as a source, withdrawn while empty, offered once stocked, a read spends a tool call |
| `tests/test_db_forecasts.py` | `delete_forecast` removes research, takes updates, keeps the run, 404s |
| `tests/test_api_forecasts.py` | `DELETE /forecasts/{id}` end to end |

## Two numbers to read off the first real run

1. **How often `search_research` is called, per stage.** ADR 77 measured three tools at zero
   calls. Near zero here means the fix is the instructions, not the store.
2. **Which ceiling a cell hits first — tokens or tool calls.** Two more tool calls against an
   unchanged `total_tokens_limit`. Tokens binding first is intended; a cell dying on
   `total_tokens_limit` mid-write is not.
