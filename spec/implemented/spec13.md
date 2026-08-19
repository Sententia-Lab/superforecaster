# Spec 13 — Search through Tavily's MCP server

## The problem

`superforecaster/tools.py` reached Tavily through a hand-rolled `httpx` POST. Tavily now runs
a hosted MCP server that serves four tools — search, extract, map, and crawl — so the custom
client is code we maintain for one of the four.

A straight swap does not work. `search_web` does three jobs and the MCP server does one:

| Job | `tools.search_web` | Tavily MCP |
|---|---|---|
| Clamp results to `as_of` | `_tavily_body` sets `end_date` + `topic="news"` | `tavily-search` takes `start_date`/`end_date` |
| Prove the clamp held | `_drop_leaked` drops results newer or undated | impossible — the server discards `published_date` |
| Record every URL seen | `deps.sources_seen.extend(refs)` | nothing |

Job 2 is clamp 1 of ADR 17. Job 3 feeds the run tree, `check_citations`, and the leak audit.

## Where it sits

```
agents/{outside_view,inside_view,critic,update,resolution,postmortem}.py
    tools=[search_wikipedia, ...]            unchanged Python tools
    toolsets=[tavily_mcp.web_search_toolset] the web search, chosen per run
```

The six agents that search lost `search_web` from `tools=[...]` and gained the toolset. The
five that never searched — `draft`, `decompose`, `lenses`, `reflect`, `synthesize` — are
untouched.

## Data lineage

### A live search

```
model calls search_web(query="fed rate cut odds")
  -> RenamedToolset.call_tool                    "search_web" -> "tavily-search"
  -> MCPToolset.call_tool
  -> tavily_mcp.process_tool_call(ctx, call_tool, name, args) -> ToolResult
```

```json
{ "step": "_capped", "in":  {"query": "fed rate cut odds", "max_results": 20, "search_depth": "advanced"},
                     "out": {"query": "fed rate cut odds", "max_results": 5, "search_depth": "basic",
                             "include_raw_content": false, "include_images": false} }
```

```json
{ "step": "await call_tool", "out": "Detailed Results:\n\nTitle: Fed holds rates steady\nURL: https://example.com/fed-holds\nContent: The committee left the target range unchanged.\n" }
```

```json
{ "step": "parse_sources", "writes": "deps.sources_seen",
  "out": [{"url": "https://example.com/fed-holds", "title": "Fed holds rates steady",
           "query": "fed rate cut odds", "published_date": null, "tool": "search_web", "as_of": null}] }
```

From there nothing is new — the same list every tool has always written:

```
deps.sources_seen
  -> runner._make_event_handler   diff on FunctionToolResultEvent -> events.Source
  -> app/stream.py _source_payload -> SSE frame
```

```json
{ "type": "source", "sub_question": "sq2",
  "payload": {"url": "https://example.com/fed-holds", "domain": "example.com",
              "title": "Fed holds rates steady", "query": "fed rate cut odds",
              "published_date": null, "tool": "search_web", "credibility": null} }
```

```
  -> stages.py:176,213        sources=list(cdeps.sources_seen)
  -> BaseRateStepPayload      -> db.finish_step        [writes: run_steps.payload_json]
  -> checks.check_citations   retrieved = {ref.url ...}   principle 4 gate
```

### A backtest search

Unchanged. `web_search_toolset` returns `FunctionToolset([tools.search_web])`, which POSTs to
`api.tavily.com/search` and runs `_drop_leaked`, so `published_date` is real:

```json
{"url": "https://reuters.com/x", "title": "...", "query": "russia ukraine",
 "published_date": "2022-01-14T00:00:00Z", "tool": "search_web", "as_of": "2022-02-01T00:00:00Z"}
```

## Changes, file by file

| File | Change |
|---|---|
| `superforecaster/tavily_mcp.py` | **new.** `web_search_toolset`, `process_tool_call`, `parse_sources`, `mcp_search`, `_capped`, `_server_url`, `_toolset` |
| `superforecaster/tools.py` | `find_disconfirming_evidence` branches to `mcp_search` on a live run. `search_web` and every helper above it unchanged |
| `superforecaster/config.py` | `Settings.tavily_mcp_url`, `DEFAULT_TAVILY_MCP_URL` |
| `superforecaster/runner.py` | `_QUERY_ARG_NAMES` gains `url` and `urls`; `_tool_query_arg` renders a list |
| six `agents/*.py` | `search_web` moves from `tools=[...]` to `toolsets=[web_search_toolset]` |
| `pyproject.toml` | dev group gains `fastmcp-slim[server]` so tests can stand a fake Tavily up in-process |
| `.env.example` | `TAVILY_MCP_URL`, commented out |

### Signatures

```python
def web_search_toolset(ctx: RunContext[ForecastDeps]) -> AbstractToolset[ForecastDeps] | None
async def process_tool_call(ctx, call_tool: CallToolFunc, name: str, args: dict) -> ToolResult
def parse_sources(text: str, *, query: str, tool: str) -> list[SourceRef]
async def mcp_search(ctx: RunContext[ForecastDeps], query: str) -> str
def _capped(name: str, args: dict) -> dict
```

## Decisions

- **`tavily-search` is renamed to `search_web`.** Every prompt names that tool, `SourceRef.tool`
  carries it to the frontend, and `attach_budget` tells the agent to stop calling it by name.
  One `RenamedToolset` keeps all of them true.
- **Extract, crawl, and map are live-only.** None takes a date filter, so each is an
  uncontrolled leak in a backtest. They fall out of the same branch that picks the toolset.
- **Arguments are capped in `process_tool_call`, not asked for in a prompt.** `BUDGETS` counts
  tool calls, not tokens, so one uncapped crawl could spend a cell's whole token budget on a
  single call.
- **No key means no tool offered**, rather than a tool that returns an explanatory string. An
  agent cannot waste a call on a tool it cannot see. `GET /config` still reports
  `search_enabled` to the operator.

ADR 75 records the reasoning. ADR 17 is not superseded.

## Tests

`tests/test_tavily_mcp.py` — 16 tests, no network. An in-process `FastMCP` server answers in
Tavily's text format, so the path runs through a real `MCPToolset`.

Two of them are the ones that matter:

- `test_a_backtest_gets_the_audited_http_tool_not_the_mcp_server` — the branch ADR 17 rests on.
- `test_a_spent_budget_withdraws_the_mcp_tool_too` — ADR 69 still holds now that the tool
  arrives from a toolset rather than `tools=[...]`.

`tests/test_tools_backdating.py` gained two tests pinning which search path
`find_disconfirming_evidence` takes in each direction.

## Not done

`_capped` bounds crawl and map on numbers read from the docs, not from a live run. Check them
against real responses once the server has been used in anger.
