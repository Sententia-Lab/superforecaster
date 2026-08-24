# Spec 16 — Codebase simplification

Status: implemented (branch `claude/codebase-simplification-0c8a1e`, 2026-08-21).
Decisions: ADR 80, 81, 82, 83, 84. Superseded: ADR 12 (update cycle), 17, 18, 62, 68, 69, 78.

## 1. What problem does this solve?

One forecast run cost $1–5. An audit found three causes:

- The 18 research cells caused ~93% of the cost. ~90% of a cell's input was the
  transcript, re-sent on every model request. Nothing was cached, because the budget
  line in the system prompt changed every turn and the tool list changed one turn in.
- The synthesis agent re-typed its own inputs (`decompositions`, `research`, four
  question fields) as output tokens at $15/MTok. Code overwrote four of them at once.
- ~1,900 lines of code were dead or reachable only from tests, and the backtest clamps
  (`forecast_date`, `model`, the model garden) were never set on any production path.

The codebase was also hard to scan: eleven agents each carried a `build_` / `get_` /
`with_model` triple, and long docstrings repeated what `spec/ADR.md` already records.

## 2. Where does the change sit?

Everywhere in `backend/`. No frontend code changed. The API surface is unchanged except
one stream frame: the `result` frame is gone (the `run` frame supersedes it, and the
frontend never read `result`).

Size: −9,525 / +2,040 lines over 81 files. Non-test backend code went from ~12.0k to
6.9k lines. The test suite went from 470 to 374 tests, all green.

## 3. What changed, file by file

### Deleted

| Path | Reason |
|---|---|
| `backend/test_forecasting_baseline/` | nothing imported it |
| `backend/app/evals/components.py` + 3 models + CLI `test` command | the case directory does not exist; always 0 cases |
| `backend/superforecaster/model_garden.py` + `.json` | backtest clamp, never set in production (ADR 80) |
| `backend/superforecaster/scoring.py` — **kept** | still serves `/calibration` and `resolve` |
| `backend/superforecaster/tools/dates.py` | only the removed clamps used it |
| `backend/app/stream.py` | folded into `superforecaster/events.py` |
| `checks.py`: `aggregate_source_confidence` cluster, `base_rate_spread` | tests-only |
| `models.py`: `GoldenQuestion`, `QuestionScore`, `Scorecard`, `ModelEntry`, `ResearchSummary`, `Confidence` alias | unused, or replaced (ADR 82) |
| deps `anthropic`, `python-multipart`, `freezegun`, `pydantic-graph` import | unused / transitive |

### Tools — `superforecaster/tools/` (ADR 83)

Four tools: `search_research`, `search_web`, `extract_pages`, `search_wikipedia`.
`crawl_site`, `map_site`, and `find_disconfirming_evidence` are gone. Tools return
field-filtered dicts; Pydantic AI serialises them, so `_json` is gone.

### Budget and caching — `superforecaster/config.py`, `agents/__init__.py` (ADR 81)

```python
@dataclass(frozen=True, slots=True)
class Budget:
    name: str
    tool_calls: int   # withdraw_tools removes every tool once this is spent
    requests: int     # UsageLimits.request_limit
    tokens: int       # UsageLimits.total_tokens_limit
```

- The per-request three-band instruction, the cost ceiling, and `genai_prices` are gone.
- One static sentence at the end of the user prompt names the tool-call budget.
- `tool_calls_limit = tool_calls + 4`: the model can batch calls into one turn, a batch
  is refused whole, and withdrawal is the real cap.
- `get_model_settings()` sets `anthropic_cache_tool_definitions`,
  `anthropic_cache_instructions`, and `anthropic_cache`. The system prompt and the tool
  list never change inside a run, so the prefix and the transcript stay cached.
- `search_research` is always offered; an empty store answers "Nothing stored yet".
- Research cells: 8 tool calls, 12 requests — the depth the old countdown allowed.

### Agents — `superforecaster/agents/*.py` (ADR 82)

Each module holds `INSTRUCTIONS`, one module-level `agent` built without a model, and
`run_<n>(...)`. `runner.run_agent` supplies the model per call:

```python
run_agent(agent, prompt, *, budget, deps, model=None, timeout=None, run_name) -> result
```

Agent outputs carry only what the agent decides; code stamps identity:

| Agent | Output type | Code stamps |
|---|---|---|
| base_rate_cell | `BaseRateResult{evidence, analogs, disagreement}` | the chosen `Lens` fields, `sub_question_ids` |
| inside_view | `AdjustmentResult{adjustments, steel_man}` | `lens_name`, `sub_question_ids` |
| synthesize | `ForecastAnswer{probability, reasoning, extreme_justification}` | question fields, `decompositions` |

The synthesis prompt renders the three views as short lines (`agents/synthesize.py
render_views`), not three `model_dump_json(indent=2)` dumps. `decompose`, `lenses`, and
`draft` have no tools. `format_history` caps update history at the last 10 entries.
`check_linkage` keeps its first arm only; `SYNTHESIS_FIXABLE = {"derivation",
"calibration_hygiene"}`. The whole-question fallback paths are gone: a run with no
measured base rate fails at synthesis with a plain error.

### Update cycle — `superforecaster/update.py` (ADR 84)

`pydantic_graph` and the four node classes became one function:

```python
run_update_cycle(record: ForecastRecord, deps) -> UpdateOutcome
# resolution check -> early return if resolved
# update agent -> verify once if the move is large -> noise / consistency gate
```

### App layer

| File | Change |
|---|---|
| `app/db.py` | migration 7 drops `forecasts.research_json`; `_row_to_*` are `dict(row)` / `model_validate`; the timestamp converter makes values aware; DDL is a module-level `_SCHEMA` |
| `app/cli.py` | commands hold their own bodies (no `_run`/`SimpleNamespace`); `--fixture` is a boolean flag plus `--fixture-path`; `models` and `test` commands gone |
| `app/evals/__init__.py` | `eval_main(name, build_dataset, make_task, argv)` — the shared command line |
| `api/runs.py` | the `result` frame is gone; `_failure_hint` handles the machine errors |
| `superforecaster/events.py` | the four event dataclasses plus `frame(event, sub_question)` — the SSE shape |

## 4. Data lineage — one base-rate cell, after the change

```
machine.execute_step (stage base_rates, sq1, lens "USTR Section 301 actions")
  -> stages.run_base_rate_step(input, sub_question, lens, deps)
       -> run_agent(outside_view.agent, prompt + search_note, budget=8/12/200k,
                    model=resolve_agent_model())
```

The model calls a tool; the tool returns dicts, records sources, fills the store:

```json
[{"title": "Section 301 tracker", "url": "https://ustr.gov/301",
  "content": "USTR initiated seven investigations between 2017 and 2024 ..."}]
```
`[writes: research_docs via store.remember; appends: deps.sources_seen]`

The agent answers with a `BaseRateResult` (nothing else):

```json
{"evidence": [{"kind": "counted", "hits": 2, "n": 7,
               "note": "301 investigations 2017-2024 that produced tariffs"}],
 "analogs": [{"description": "China tech transfer 2017", "outcome": 1.0}],
 "disagreement": "Population mixes tariff and export-control actions."}
```

`run_base_rate_step` stamps the chosen lens onto it and persists the payload:

```json
{"lens": {"name": "USTR Section 301 actions", "population": "...", "weight": 0.45,
          "weight_rationale": "...", "why_it_fits": "...", "sub_question_ids": ["sq1"],
          "evidence": [...], "analogs": [...]},
 "disagreement": "...", "sources": [{"url": "https://ustr.gov/301", "tool": "search_web"}]}
```
`[writes: run_steps.payload_json]`

At synthesis, the agent returns only:

```json
{"probability": 0.167, "reasoning": "Anchor 0.273 ...", "extreme_justification": ""}
```

`run_synthesis_stage` stamps the question fields and the decomposition onto `Forecast`,
runs the checks, and `save_forecast` writes:

```json
{"id": "290b27f3-...", "question": "Will the Fed cut ... ?", "probability_row": 0.167,
 "decompositions_json": "[{\"id\": \"sq1\", ...}]", "research_id": "b1c2..."}
```
`[writes: forecasts, forecast_updates — no research_json since migration 7]`

## 5. Measured effect

- 374 tests pass. `make smoke` completes end to end (p = 0.343, one retry).
- Logfire, per research-cell request: 80–90% of input tokens are cache reads; the only
  misses are the first request and the final one (tool withdrawal changes the prefix).
- Migration 7 verified against a copy of the production database; an old run's detail
  still renders, and the UI drains a full gated run with no console errors.
- Cost per request fell ~40%; the cell budget (8 searches) holds the request count at
  the old countdown's effective depth.
