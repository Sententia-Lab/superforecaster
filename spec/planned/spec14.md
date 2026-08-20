# Spec 14 — Configurable Tavily search

## The problem

`search_web` takes one argument. Tavily's search takes eighteen. The agent cannot restrict a
search to a date range it chose, exclude a domain that keeps returning noise, or ask for an
exact phrase. Everything is fixed at values one engineer picked.

Opening all eighteen to the agent is the opposite error. Some of them decide correctness, not
preference — `start_date` and `end_date` are the backtest clamp, and an agent that omits them
reads the future. Others decide cost: one search with `include_raw_content` returns 20,114
characters where the snippet returns 1,502.

So the feature is not "expose the parameters". It is **decide, per parameter, who owns it**.

## Where it sits

```
model decides ──→ search_web(ctx, query, ...agent args)
                        │
                        ▼
                  resolve_search_args(agent_args, deps, settings, searches_left) -> dict
                        │                    ▲        ▲            ▲
                        │            forecast_date  engineer   auto_parameters
                        │              clamp        + user       gating
                        │                           blocklist
                        ▼
                  AsyncTavilyClient.search(**kwargs)
                        │
                        ▼
                  _drop_leaked ──→ SourceRefs ──→ deps.sources_seen
                  usage.credits ──────────────→ deps.tavily_credits
```

`resolve_search_args` is the whole design. It is pure — arguments in, dict out, no network, no
clock — so every rule below is asserted directly, the way `_tavily_body` is tested today.

## Six kinds of parameter

Every Tavily argument is exactly one of these. The kind decides who may set it.

| Kind | Rule | Parameters |
|---|---|---|
| **Fixed** | a constant; not in the tool signature at all | `include_answer=False`, `include_raw_content=False`, `include_images=False`, `include_image_descriptions=False`, `include_favicon=True`, `include_usage=True` |
| **Clamped** | comes from `deps.forecast_date`; overrides the agent | `start_date`, `end_date`, and `topic` forced to `"news"` |
| **Bounded** | the agent proposes, the system caps | `max_results` ≤ 5, `search_depth` ∈ {basic, fast}, `chunks_per_source` ≤ 3 |
| **Free** | the agent decides | `include_domains`, `topic` (live runs only) |
| **Derived** | read off another argument; not in the signature | `exact_match`, from whether `query` holds a quoted phrase |
| **Layered** | system + user + agent, merged | `exclude_domains` |
| **Conditional** | the agent decides, but dropped when a precondition fails | `country` (needs `topic="general"`), `time_range` (illegal when clamped), `auto_parameters` (needs budget headroom) |

**`exact_match` moved from Free to Derived (2026-08-20).** As a free parameter it has two
halves that must agree, and the agent has no reason to connect them: Tavily errors on
`exact_match=true` without a quoted phrase, and ignores the quotes without the flag. A live
`base_rate_cell` run quoted three revenue figures, got no exact filter, and came back with
an unrelated company. `search_web` now reads the flag off the query, so the `query`
description is true as written and the error is unreachable.

**Fixed parameters are not in the signature.** Every parameter in the signature is a field in
the JSON schema the model reads on every request, and one it can set. A fixed parameter in the
signature costs tokens to read, invites the model to set it, and is then silently overridden.
ADR 76 recorded this: keeping a value out of the signature *is* the enforcement.

### The signature

```python
async def search_web(
    ctx: RunContext[ForecastDeps],
    query: str,
    topic: Literal["general", "news", "finance"] | None = None,
    time_range: Literal["day", "week", "month", "year"] | None = None,
    max_results: int | None = None,
    search_depth: Literal["basic", "fast"] | None = None,
    chunks_per_source: int | None = None,
    include_domains: Sequence[str] | None = None,
    exclude_domains: Sequence[str] | None = None,
    country: str | None = None,
    exact_match: bool | None = None,
    auto_parameters: bool | None = None,
) -> str:
```

Eleven arguments the agent controls, six constants it never sees.

### Resolution order

Last write wins, so nothing the agent sends can reach past step 4.

```python
def resolve_search_args(
    agent: dict, deps: ForecastDeps, settings: Settings, searches_left: int
) -> dict:
    args = {k: v for k, v in agent.items() if v is not None}   # 1. agent's request
    args |= _bounded(args)                                     # 2. caps
    args = _drop_illegal(args, deps, searches_left)            # 3. conditionals
    args["exclude_domains"] = _merge_blocklists(args, settings) # 4. layers
    args |= _clamp(deps.forecast_date)                         # 5. deps override
    return args | FIXED                                        # 6. constants override
```

## The three rules that are code, not documentation

### 1. `end_date` alone does nothing

Measured 2026-08-19 across three queries at `forecast_date = 2022-02-01`:

| Sent | Results published after the cutoff |
|---|---|
| `topic="news"`, `end_date` | **15 of 15** |
| `topic="news"`, `start_date`, `end_date` | **0 of 15** |

Nothing errors either way. `_clamp` therefore always emits all three:

```json
{"topic": "news", "start_date": "2012-02-04", "end_date": "2022-02-01"}
```

`start_date` is `forecast_date - BACKTEST_WINDOW_DAYS` (3650, ten years — a reference class
wants depth; the width does not affect whether the filter applies, only its presence does).

`_drop_leaked` stays regardless. This is the second time Tavily's date handling has not matched
its documentation, so the second guard is verification, not redundancy (ADR 17).

### 2. `time_range` and dates are mutually exclusive at the API

```
BadRequestError: When time_range is set, start_date or end_date cannot be set
```

Same for `days`. So `_drop_illegal` removes `time_range` whenever `forecast_date` is set — and
`time_range` is measured from *now*, not from `forecast_date`, so in a clamped run it is
meaningless as well as illegal. `days` is not in the signature at all.

### 3. `country` needs `topic="general"`, which a backtest cannot have

A clamped run forces `topic="news"`, because news is the only topic that returns
`published_date`, which `_drop_leaked` needs. So `country` is unusable in a backtest by
construction. `_drop_illegal` removes it and the tool appends one line to its result saying
so — a silent drop makes the agent retry the same call.

## Domains: blocklist is shared, allowlist is the agent's alone

`include_domains` is a restriction, not a preference. A non-empty list means the agent can
search **nowhere else**, so a system-wide allowlist would cap every reference class the
forecaster could ever find. It stays per-call and agent-only.

`exclude_domains` merges three sources, none able to remove another's entries:

```python
exclude_domains = TAVILY_BLOCKLIST + settings.tavily_exclude_domains + (agent or [])
```

| Layer | Where | Changed by |
|---|---|---|
| `TAVILY_BLOCKLIST` | a constant in `tavily_tools.py` | backend engineers, in review |
| `settings.tavily_exclude_domains` | `TAVILY_EXCLUDE_DOMAINS`, comma-separated | whoever deploys |
| the `exclude_domains` argument | the tool call | the agent, per search |

The comma-separated env format matches `BUDGET_<AGENT>` in `config.py:251`, the only existing
list-valued variable.

## `auto_parameters` is gated, not discouraged

It lets Tavily override `search_depth` and others, so it is a cost multiplier the agent
controls. A prompt cannot enforce "sparingly". `_drop_illegal` removes it unless
`searches_left > SEARCH_RESERVE`, which is the same headroom test `attach_budget` already uses
for its warning bands. An agent near the end of its budget cannot spend the remainder on a
deeper search than it can afford.

## Favicons reach the UI

`include_favicon=True` puts one on every result:

```json
{"url": "https://fred.stlouisfed.org/series/FEDFUNDS",
 "title": "Federal Funds Effective Rate",
 "favicon": "https://fred.stlouisfed.org/favicon.ico",
 "content": "The federal funds rate is ...",
 "score": 0.94}
```

That is a four-place change, none of it local to the tool:

```
SourceRef.favicon: str = ""        models.py:706
  ← set in search_web
  → stream._source_payload         app/stream.py:35
  → SourceList.jsx                 renders beside the title
```

## Usage counts against the budget

`include_usage=True` returns credits, not dollars:

```json
{"usage": {"credits": 1}}
```

Pay-as-you-go is **$0.008 per credit** (tavily.com/pricing, read 2026-08-19). Treat that as a
prior — it is the public list price, not this account's effective rate.

### Lineage

```
search_web
  payload["usage"]["credits"] == 1
  -> deps.tavily_credits += 1                    [ForecastDeps, per-cell private]
  -> agents.spent_usd(model, usage, credits)
       llm     = calc_price(...).total_price     0.0412
       tools   = 1 * TAVILY_CREDIT_USD           0.0080
       total                                     0.0492
  -> attach_budget raises when total >= budget.cost_usd
```

`deps.tavily_credits` must be **per-cell private**, for the same reason `sources_seen` is
(`deps.py:31`): cells run concurrently, and a shared counter mixes them. `cell_deps` already
resets `sources_seen`; it resets this too, and `stages.py` sums both at the barrier.

### Re-baselining, which is required in the same change

`spent_usd` currently sees LLM cost alone, and the ceilings were set against that. Adding
credits makes every ceiling bind earlier. New ceiling = old + `tool_calls × credits/call ×
$0.008`, rounded up to the nearest cent:

| Agent | tool_calls | Worst-case credits | Added | `cost_usd` |
|---|---|---|---|---|
| `base_rate_cell` | 8 | 8 | $0.064 | 0.40 → **0.47** |
| `inside_view` | 8 | 24 ¹ | $0.192 | 0.40 → **0.60** |
| `decompose` | 4 | 4 | $0.032 | 0.15 → **0.19** |
| `lenses` | 2 | 2 | $0.016 | 0.15 → **0.17** |
| `critic` | 3 | 3 | $0.024 | 0.10 → **0.13** |
| `resolution`, `postmortem` | 4 | 4 | $0.032 | 0.10 → **0.14** |
| `update` | 4 | 12 ¹ | $0.096 | 0.10 → **0.20** |

¹ `find_disconfirming_evidence` runs three searches inside one tool call. The budget charges
one call and Tavily charges three credits, so any agent holding that tool needs 3× headroom.
This is the first time that difference has cost anything.

`reflect`, `synthesize`, and `draft` have no tools and do not change.

## `as_of` becomes `forecast_date`

95 references across 24 files. Mechanical, and worth doing while this code is open: `as_of`
does not say what date it is, and the eval cases carry a `forecast_date` and a
`resolution_date`.

| Now | After |
|---|---|
| `ForecastDeps.as_of` | `ForecastDeps.forecast_date` |
| `SourceRef.as_of` | `SourceRef.forecast_date` |
| `agents.as_of_note` | `agents.forecast_date_note` |
| `model_garden.pick_clean_model(as_of)` | `pick_clean_model(forecast_date)` |
| `EvalCase.as_of` | `EvalCase.forecast_date` |

`resolution_date` already exists on `ForecastInput`. The two live on different objects —
`forecast_date` is when the forecast is made, `resolution_date` is when the question settles —
and a comment on each should say so, because the names now invite confusion.

## Files

| File | Change |
|---|---|
| `superforecaster/tools/tavily_tools.py` | the signature, `resolve_search_args` and its four helpers, `FIXED`, `TAVILY_BLOCKLIST`, `BACKTEST_WINDOW_DAYS`, credit capture |
| `superforecaster/config.py` | `Settings.tavily_exclude_domains`, `TAVILY_CREDIT_USD`, eight re-baselined `Budget` rows |
| `superforecaster/deps.py` | `forecast_date` rename, `tavily_credits` counter |
| `superforecaster/models.py` | `SourceRef.favicon`, `SourceRef.forecast_date` |
| `superforecaster/agents/__init__.py` | `spent_usd` takes credits; `forecast_date_note` |
| `superforecaster/stages.py` | sum `tavily_credits` at the cell barrier |
| `app/stream.py` | `favicon` in `_source_payload` |
| `frontend/src/components/SourceList.jsx` | render the favicon |
| `superforecaster/agents/*.py` (8) | `forecast_date_note` import |

## Tests

`tests/test_search_args.py` — new, and the bulk of the value, because `resolve_search_args` is
pure:

- every Fixed value survives an agent that passes the opposite
- `max_results=50` resolves to 5; `max_results=2` stays 2
- clamped → `start_date` **and** `end_date` **and** `topic="news"`, always all three
- clamped → `time_range` dropped even when the agent sent it
- `country` dropped when `topic != "general"`, and the result says why
- `auto_parameters` dropped when `searches_left <= SEARCH_RESERVE`
- `exclude_domains` is the union of all three layers; no layer can remove another's
- `include_domains` passes through untouched — the agent owns it

`tests/test_tools_backdating.py` — the clamp tests move to the new resolver.
`tests/test_config.py` — a test that every agent holding tools has `cost_usd` above its
worst-case credit spend, so a future budget edit cannot make the ceiling unreachable.

## Verification

```bash
cd backend && uv run pytest
```

Then one clamped and one live search against the real API, checking the four claims this spec
rests on:

1. a clamped search returns results **inside** the window, not zero results withheld
2. `usage.credits` arrives and lands on `deps.tavily_credits`
3. every `SourceRef` carries a favicon
4. `country` with `topic="news"` is dropped and the agent is told

Then one full forecast, watching the run tree for favicons on the source chips and Logfire for
a `spent_usd` that now exceeds the LLM-only figure by roughly `credits × $0.008`.

## Open, deliberately

`TAVILY_CREDIT_USD = 0.008` is the public pay-as-you-go price, not a measured effective rate.
After the first week of runs, compare accumulated credits against the Tavily dashboard and
correct it. The re-baselined ceilings above move with it.
