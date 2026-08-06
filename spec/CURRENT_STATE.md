# Current State

Every module, function, and model that exists today. Generated against the code, not memory.

Read this to answer "what is there and what does it do." Read `spec/ADR.md` for *why* it is
shaped this way. Read `spec/superforecasting_methodology.md` for the 16 principles the code
implements — `P<n>` throughout this document refers to them.

Last regenerated: 2026-08-06, after the gated rebuild (`spec/implemented/spec5.md`,
ADRs 45–48).

---

## Current shape

The forecast pipeline is a **persisted state machine of user-gated stages**. A run is a row
in `gated_runs` plus one `run_steps` row per stage cell; nothing executes unless a user
asks for that specific step. Five stages, gated in order:

```
decompose (1) → lenses (per researchable sub-question) → base_rates (per sub-question × lens)
              → inside_view (same cells) → synthesis (1)
```

- `machine.py` decides every legal transition; `stages.py` holds the per-stage forecast
  functions; `db.py` reads and writes rows. `stages.run_all` drives the same functions
  back-to-back with no gates for the CLI and evals — one implementation of the
  methodology, not two (ADR 45).
- `POST /runs/{id}/steps/{step_id}/stream` **is** the step: the SSE response executes the
  agent inside its own generator, and a client disconnect cancels the step, landing it as
  `error='cancelled'`, immediately claimable again (ADR 46).
- One agent step in flight per process (`machine._slot`); a run idling at a gate costs
  nothing, and there is deliberately no whole-run timeout. The bounded thing is one step:
  `AGENT_TIMEOUT_SECONDS` (180) per agent call, `STAGE_TIMEOUT_SECONDS` (600) per step.
- Every step's output is a typed payload `model_dump_json`'d into its row, so the whole
  reasoning trail survives restart and reload — `GET /runs/{id}` rebuilds the entire view
  from SQLite alone.
- The frontend is React 18 + Vite (`frontend/`), built to `frontend/dist` and served as
  static files by FastAPI (ADR 47).
- Community features (public questions, votes, digest) are removed; migration v2 dropped
  their tables (ADR 48). The backlog is now a `gated_runs` row with `status='backlog'`.

Deleted in the rebuild: `superforecaster/runs.py`, `eventstream.py`, `durability.py`,
`graphs/forecast.py`, the DBOS dependency, the ring buffer/replay apparatus, the
watcher-grace watchdog, and the 2,059-line hand-rolled frontend renderer.

Preserved untouched: `checks.py`, `scoring.py`, all agents, `tools.py`, `deps.py`,
`observability.py`, the model garden, the component evals, `graphs/update.py`, and the
daily refresh cron.

---

## Repository layout

```
backend/
  config.py                    # env loading, typed settings, budgets, timeouts
  api/
    main.py                    # FastAPI app; startup preflight; serves frontend/dist at /
    deps.py                    # require_admin, is_local_mode (localhost = no token)
    runs.py                    # gated run CRUD + the step stream endpoint
    questions.py               # POST /questions/draft, /critique
    forecasts.py               # forecast reads + admin writes + single refresh
    calibration.py             # GET /calibration
    admin.py                   # POST /admin/refresh/run
  superforecaster/
    __main__.py                # typer CLI: forecast/refresh/resolve/critique/postmortem/
                               #   models/diagram/test/config/serve
    machine.py                 # gated-run state machine: start_run, advance, execute_step
    stages.py                  # per-stage forecast functions + run_all (no gates)
    db.py                      # SQLite; forecasts + gated_runs/run_steps; migrations
    models.py                  # every Pydantic model
    checks.py                  # the 16 principles as pure functions over typed output
    scoring.py                 # time-weighted probability, Brier, calibration (pure)
    errors.py                  # AgentTimeout, StageTimeout
    deps.py                    # ForecastDeps, SearchBudget (per-cell budget)
    tools.py                   # search_web (Tavily), search_wikipedia, as_of clamps
    observability.py           # logfire config, run_agent wrapper, event handler
    cron.py                    # APScheduler daily refresh over the update graph
    model_garden.py/.json      # model registry with training cutoffs (backtest clamp 2)
    graphs/
      update.py                # the ONLY graph left: resolution check + Bayes update
      state.py                 # UpdateState
    agents/                    # eleven agents, one module each (see Module reference)
    evals/components.py        # per-agent eval harness (+ components/*.json cases)
    fixtures/                  # CLI fixture JSONs
  tests/                       # 285 tests, all passing (see Tests)
  pyproject.toml               # uv-managed; Python >= 3.12
  Dockerfile
frontend/
  package.json                 # react, react-dom; dev deps vite, @vitejs/plugin-react
  vite.config.js               # dev proxy of API routes to :8099 (VITE_API_PROXY_TARGET
                                #   overrides, for the dockerized dev server); build → dist/
  Dockerfile.dev              # dev-only: npm run dev in a container, not part of the
                               #   default docker compose up (ADR 47 keeps prod one-process)
  src/
    main.jsx, App.jsx          # shell: header, sidebar, main pane, theme toggle
    api.js                     # fetch wrappers + streamStep (fetch/ReadableStream SSE)
    derive.js                  # pure derivations mirroring checks.py
    theme.css                  # the --pv-* design tokens, light + dark
    hooks/useRuns.js           # sidebar list, refetch on demand
    hooks/useStepStream.js     # one step stream at a time; AbortController = lifetime
    components/                # Sidebar, NewForecastView, BacklogView, RunView,
                               #   FieldEditor, StepControls, LensCard, CellActivity,
                               #   LiveTail, SynthesisSection
.github/workflows/ci.yml       # push/PR: uv run pytest + npm run build
scripts/hooks/pre-push         # runs backend tests; enable with core.hooksPath
docker-compose.yml             # api service (frontend/dist bind-mounted); dev-only
                                #   frontend service behind the "dev" profile
spec/                          # ADR.md, this file, methodology, implemented/, planned/
```

---

## How to run

```bash
cd backend && uv sync
export ANTHROPIC_API_KEY=sk-ant-...   # or PYDANTIC_AI_GATEWAY_API_KEY
export TAVILY_API_KEY=tvly-...        # optional; Wikipedia-only without it
uv run python -m superforecaster serve          # API + built UI on :8000
```

- Frontend dev loop: `cd frontend && npm install && npm run dev` (Vite proxies API routes
  to FastAPI on 8099, matching `.claude/launch.json`). Production: `npm run build`, then
  FastAPI serves `frontend/dist` at `/`.
- Tests: `cd backend && uv run pytest` — 285 pass, no network, no API keys.
- Pre-push hook: `git config core.hooksPath scripts/hooks` (runs pytest before push).
- CI (`.github/workflows/ci.yml`): backend `uv run pytest -q` and frontend `npm ci &&
  npm run build`, on push to main and on PRs.
- Docker: `docker compose up` — build `frontend/dist` first; it is bind-mounted read-only
  and served by the API container.
- Docker frontend dev: `docker compose --profile dev up` also starts a `frontend` service
  (`frontend/Dockerfile.dev`, hot-reloading Vite on :5173) proxying to the `api` service via
  `VITE_API_PROXY_TARGET=http://api:8000`. Not part of the default `docker compose up` —
  production stays one process, per ADR 47.

### CLI (`uv run python -m superforecaster <cmd>`, typer)

| Command | Does |
|---|---|
| `forecast` | full pipeline, no gates (`stages.run_all`), saves unless `--no-save` |
| `refresh` | update graph by `--id`, or `run_update` on a `--fixture` |
| `resolve` | resolution check by `--id` or `--fixture` |
| `critique` | P3 resolvability review of a question + criteria |
| `postmortem` | P13 review of a resolved forecast |
| `models` | model garden `list` / `probe` / `pick --as-of` |
| `diagram` | mermaid: gated stage machine (`forecast`) or update graph (`update`) |
| `test component [agent]` | component evals (`e2e` is spec4, not built) |
| `config` | every setting and whether it came from env, `.env`, or nowhere |
| `serve` | uvicorn on `127.0.0.1:8000` (`--host`, `--port`, `--reload`) |

---

## The gated run machine

**Run status** is `backlog → active → complete`. Error is a nullable field on the run
(the sidebar's red chip), not a status — a run with a failed step has gone nowhere, and
claiming the step again (retry) clears it.

**Step status** is `pending → running → complete | error`, with `error → running` on
retry (a CAS in `db.claim_step`, which also increments `attempts`).

**Transitions** (`machine.py`):
- `start_run` — the four-field gate (`question`, `resolution_criteria`,
  `resolution_source`, `resolution_date`), then `backlog → active` and one pending
  `decompose` step. Checked at start, not creation: a half-formed backlog idea is
  legitimate.
- `advance` — called after every non-synthesis step completes; materializes the next
  stage's pending rows from the just-written payloads (decompose fixes the sub-questions,
  each lenses step fixes its cells). Idempotent via `INSERT OR IGNORE` on
  `UNIQUE(run_id, stage, sub_claim_id, lens_name)`. Zero researchable sub-questions
  bypasses straight to synthesis.
- `gate_offender` — a step is claimable only when every step in every earlier stage is
  complete; returns the first offender.
- `execute_step` — global one-slot lock → claim CAS → `_dispatch` to the stage function
  under `asyncio.timeout(STAGE_TIMEOUT_SECONDS)` → persist. `CancelledError` (client hung
  up) lands as `error='cancelled'`; `AgentTimeout` and `StageTimeout` land distinctly;
  every other exception lands as `TypeName: message`. A completed synthesis step saves the
  forecast (`db.save_forecast`) and completes the run.
- `detail` — run + steps with payloads parsed; what `GET /runs/{id}` returns.
- `busy` — true while any step is in flight in this process.

**Restart sweep**: `db.init_db` calls `mark_interrupted_steps`, flipping any
still-`running` step to `error='interrupted by restart'` (a step can only be running while
a live connection executes it).

**Stage functions** (`stages.py`) carry the old graph's code-stamped invariants:
- `run_lenses_stage` never receives a rate — populations are chosen blind (ADR 40).
- `run_base_rate_step` re-stamps the lens identity (name, population, weight, rationale,
  `sub_claim_ids`) from the *chosen* lens, so a cell cannot re-weight its population after
  measuring it. Catches `UsageLimitExceeded` to emit an `exhausted` notice before re-raising.
- `run_inside_step` requires a measured `BaseRateStepPayload` by signature (P4 as a call
  signature); `lens_name`/`sub_claim_ids` on adjustments are stamped by code.
- `run_synthesis_stage` — arithmetic first: `checks.anchor_from` and
  `checks.implied_probability` compute the anchor and implied probability (never the
  model); reflect runs over every column's adjustments together; synthesize loops against
  `checks.run_forecast_checks` with one retry (`MAX_SYNTHESIS_ATTEMPTS = 2`); the
  question metadata is re-stamped from the caller's input. Falls back to
  `whole_question_outside` / `whole_question_adjustments` when no cells exist.
- `run_all` — the same functions back-to-back for CLI/evals; per-stage fan-out is a plain
  `gather` with `return_exceptions=True`, so a failed cell degrades instead of killing the
  run. Returns `(forecast, violations)`.

**Budgets** (unchanged from ADR 43): per-cell soft/hard search budget
(`get_cell_budget`: soft `max_iterations×1`, hard soft+3), explicit `UsageLimits` on every
agent call (AST-enforced by `test_critic_budget`), 180s per agent call, 600s per step.

---

## API endpoints

| Method + path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | — | liveness |
| `GET /config` | — | `auth_required`, `search_enabled`, `model` — the client asks the server about its own auth |
| `POST /runs` | — | create a backlog run; every field optional (201) |
| `GET /runs` | — | sidebar list, newest first, with per-stage status counts |
| `GET /runs/{id}` | — | the full persisted tree — the reload path |
| `PATCH /runs/{id}` | — | edit fields while `backlog`; 409 otherwise |
| `DELETE /runs/{id}` | admin | delete run, cascade steps (saved forecast stays) |
| `POST /runs/{id}/start` | admin | four-field gate → `active` + pending decompose; 422 names missing fields (202) |
| `POST /runs/{id}/steps/{step_id}/stream?max_iterations=N` | admin | **the gated "next"** — SSE that executes the step; 409 on gate/busy/double-claim; disconnect cancels |
| `POST /questions/draft` | — | freeform text → `DraftedQuestion` + its critique; 504 on parse timeout |
| `POST /questions/critique` | — | P3 resolvability review |
| `POST /forecasts` | admin | run the whole pipeline blocking (`run_all`), persist — API twin of the CLI |
| `GET /forecasts`, `GET /forecasts/{id}` | — | reads (`?status=active\|resolved\|ambiguous`) |
| `POST /forecasts/{id}/updates` | admin | manual probability update |
| `PATCH /forecasts/{id}/resolve` | admin | record outcome (0/1) or ambiguous (null) |
| `POST /forecasts/{id}/refresh` | admin | one update-graph cycle for one forecast |
| `GET /calibration` | — | Brier + calibration buckets over resolved forecasts |
| `POST /admin/refresh/run` | admin | trigger the daily refresh now |
| `GET /` | — | the built frontend (StaticFiles over `FRONTEND_DIR`), mounted last |

Admin = `Authorization: Bearer <ADMIN_API_KEY>`, waived for loopback requests with no
proxy headers when the key is unset (`api.deps.is_local_mode`).

**Stream frames** (`data:` JSON with a `type`): `thought` / `query` / `source` /
`exhausted` while the agent works, then `result` (the finished step) and `run` (the
updated tree including newly materialized pending steps), or `error` (with
`_failure_hint`'s one-sentence diagnosis: budget overrun → retry deeper, timeout, bad key,
rate limit). Ping every 15s.

---

## Data models (`superforecaster/models.py`)

Constants: `MAX_SEARCH_DEPTH = 50` (ceiling on a retried step's `max_iterations`).

**Forecast pipeline**
- `ForecastInput` — question, criteria, date, category, `max_iterations` (default 5).
- `SubPrediction` — one sub-question; `id` stamped by `run_decompose`; `knowability`
  (`researchable`/`judgment`).
- `Decomposition` — 3–5 `sub_claims` + `chain_rule` (`conjunction`/`disjunction`/`custom`)
  + `chain_note`.
- `Lens` — a named population chosen blind; `weight` (relevance only) is the one
  unverifiable number and carries a mandatory `weight_rationale`.
- `SubClaimLenses` — 1–3 lenses for one sub-question.
- `Evidence` — one counted or published block (`hits`/`n`); published requires a source.
- `ResearchedLens(Lens)` — `evidence` (≥1 block) + `analogs`; the rate is
  `checks.lens_rate` = Σhits/Σn, computed never asserted.
- `SubClaimBaseRates` — one researched lens + `disagreement`.
- `OutsideView` — all lenses merged; `aggregate_base_rate` computed by `checks`.
- `Adjustment` — one inside-view move (`direction`, `magnitude` ≤ 0.5, `flip_test`,
  `is_noise`); `lens_name`/`sub_claim_ids` stamped by code.
- `SubClaimAdjustments` — one cell's 1–3 adjustments + steel man.
- `Reflection` — whole-question steel man + exactly 5 `BiasCheck`s.
- `InsideView` — merged adjustments + reflection fields.
- `Forecast` — final output; `extreme_justification` required outside the calibration band.
- `GradedSource`, `HistoricalAnalog`, `SourceRef` (leakage audit; `is_leak`),
  `CheckViolation` (principle 1–16, `blocking`).

**Gated runs** (new in spec5)
- `GatedRunStatus = backlog | active | complete` — error is a field, not a status.
- `StepStatus = pending | running | complete | error`.
- `Stage = decompose | lenses | base_rates | inside_view | synthesis`.
- Step payloads: decompose → `Decomposition`; lenses → `SubClaimLenses`;
  `BaseRateStepPayload` (re-stamped lens + disagreement + sources);
  `InsideStepPayload` (lens_name + stamped adjustments + steel_man + sources);
  `SynthesisStepPayload` (reflection, outside, inside, forecast, violations, `anchor`,
  `implied`, `derivation_slack`, `attempts` — the arithmetic stored as data so the UI
  never re-derives thresholds that may have changed since the run).
- API shapes: `RunStepOut`, `GatedRunSummary` (sidebar: stage_counts, no steps),
  `GatedRunDetail(GatedRunSummary)` (full tree), `CreateGatedRunRequest` (all optional,
  `max_iterations` 1–20), `UpdateGatedRunRequest` (backlog edits).

**Drafting / critique**
- `DraftQuestionRequest` (`text`, min 20 chars) → `DraftedQuestion` → `DraftResponse`
  (parsed + critique). `CritiqueQuestionRequest` → `CriteriaCritique`
  (`is_resolvable`, `suggested_criteria`, `suggested_resolution_source` — forced
  `is_resolvable=False` when no source is named).

**Refresh / resolution / post-mortem**
- `EvidenceItem` (P11 likelihoods), `UpdateDecision`, `UpdateOutcome`,
  `ResolutionCheckResult`, `PostMortem`, `RefreshSummary`, `RefreshActionResponse`.

**DB records / scoring**
- `ForecastRecord` (+ `ForecastUpdateRecord` history), `CalibrationBucket`,
  `CalibrationReport`.
- API bodies: `CreateForecastRequest` (resolution_source required), `AddUpdateRequest`,
  `ResolveRequest`.

**Model garden / evals**
- `ModelEntry` (published `training_cutoff` — never asked of the model),
  `GoldenQuestion`, `QuestionScore`, `Scorecard`, `ComponentCase`, `ComponentScore`,
  `ComponentReport`.

Removed in the rebuild: `RunEvent`, `RunSummary`, `RunSnapshot`, `CreateRunRequest`,
`ResumeRunRequest`, `QuestionRecord`, the vote models, `ForecastRefreshResult`.

---

## Module reference

### `superforecaster/machine.py` (333 lines)
`GateError`, `BusyError`, `REQUIRED_FIELDS`, `start_run`, `advance`, `gate_offender`,
`execute_step`, `detail`, `busy`. See "The gated run machine" above.

### `superforecaster/stages.py` (316 lines)
`run_decompose_stage`, `run_lenses_stage`, `run_base_rate_step`, `run_inside_step`,
`assemble_outside` (pure fold into `OutsideView`), `run_synthesis_stage`, `run_all`.
`MAX_SYNTHESIS_ATTEMPTS = 2`.

### `superforecaster/db.py` (917 lines)
- Connection: WAL + `busy_timeout=5000` + foreign keys; ISO-8601 datetime adapters.
- `SCHEMA_VERSION = 2`; `MIGRATIONS`: v1 drops `forecast_updates.confidence`, v2 drops
  `votes`, `questions`, `refresh_runs`, `runs`. Fresh databases are stamped at the
  current version before `_migrate` runs.
- Forecasts: `save_forecast`, `add_forecast_update`, `get_forecast`, `list_forecasts`,
  `list_active_forecast_ids`, `compute_time_weighted_probability`, `resolve_forecast`,
  `mark_refreshed`, `calibration_report`.
- Gated runs (`STAGE_ORDER`): `create_gated_run`, `update_gated_run_fields` (backlog
  only), `start_gated_run` (CAS), `complete_gated_run`, `get_gated_run`,
  `list_gated_runs` (with stage counts), `delete_gated_run`, `insert_steps`
  (INSERT OR IGNORE), `claim_step` (CAS `pending|error → running`, clears the run's red
  chip), `finish_step`, `fail_step` (mirrors error onto the run), `get_step`,
  `list_steps`, `mark_interrupted_steps`.
- Errors: `NotFoundError`, `StateError`.

### `superforecaster/checks.py` (1044 lines) — unchanged
The 16 principles as pure functions. Key derivations: `lens_rate`, `adjusted_lens_rate`,
`sub_claim_rate` (relevance-weighted, never by n), `combine_sub_claim_rates` (chain rule),
`anchor_from`, `implied_probability`, `signed_adjustment`. Check battery:
`check_decomposition`, `check_base_rate_derivation`, `check_dragonfly`,
`check_aggregation`, `check_linkage`, `check_citations`, `check_signal_vs_noise`,
`check_disconfirming`, `check_bias_coverage`, `check_derivation` (±slack rule),
`check_calibration_hygiene`, `check_bayes_direction`, `check_update_magnitude`.
Entry points: `run_forecast_checks`, `run_update_checks`, `blocking`.

### `superforecaster/scoring.py` (101 lines) — unchanged
`time_weighted_probability`, `brier_score`, `calibration`. Pure; db.py calls it.

### `superforecaster/agents/` — eleven modules, unchanged prompts
`decompose.run_decompose`, `lenses.run_choose_lenses`,
`outside_view.run_research_lens` (+ `cell_deps`, `exhausted_notice`, `merge_base_rates`,
`whole_question_outside`), `inside_view.run_adjust_lens` (+
`whole_question_adjustments`), `reflect.run_reflect`, `synthesize.run_synthesize`,
`critic.run_critique`, `draft.run_draft`, `resolution.run_resolution_check`,
`update.run_update`, `postmortem.run_postmortem`. Every `run_agent` call passes explicit
`UsageLimits` (AST-enforced).

### `superforecaster/graphs/` — the update graph only
`update.py`: `CheckResolved → ApplyBayes → VerifyLargeMove → GuardUpdate`;
`run_update_graph(forecast_id)`, `update_mermaid()`. `state.py`: `UpdateState`.
The forecast graph is gone; `graphs/__init__.py` says so.

### `superforecaster/tools.py`, `deps.py`, `observability.py`, `model_garden.py` — unchanged
Tavily + Wikipedia search with `as_of` backdating clamps and per-cell budget notices;
`ForecastDeps` (`as_of`/`model` clamps, `sources_seen` audit trail, `emit` sink,
per-cell `SearchBudget`); logfire setup and the `run_agent` wrapper with
`AGENT_TIMEOUT_SECONDS` deadline; the model-cutoff registry for clean backtests.

### `superforecaster/errors.py`
`AgentTimeout` (one agent stalled) and `StageTimeout` (the step ceiling), both
`TimeoutError` subclasses. `RunTimeout` and `RunAbandoned` are deleted — a gated run is
supposed to sit idle indefinitely.

### `superforecaster/cron.py`
`run_daily_refresh` (update graph over every active forecast) + APScheduler wiring
(`start_scheduler`/`stop_scheduler`, `REFRESH_CRON_SCHEDULE`, default 06:00 UTC).
The monthly digest job is removed.

### `superforecaster/evals/components.py`
Per-agent eval harness (`run_component`, `SCORERS`). `_score_outside_row` /
`_score_inside_row` now drive the lens flow (`run_choose_lenses` →
`run_research_lens` / `run_adjust_lens` per cell) and merge with
`merge_base_rates` — the two stale imports of the old graph helpers were fixed in the
rebuild.

### `api/` — see the endpoints table
`runs.py` (230 lines) is the rewritten gated CRUD plus `stream_step`, which pre-checks
ownership/status/gate/busy as HTTP 404/409 and then runs `machine.execute_step` inside
the SSE generator via a queue, cancelling the worker task when the generator is torn
down. `main.py` runs the startup preflight banner and mounts `frontend/dist` last at `/`.

### `frontend/src/`
- `api.js` — fetch wrappers; `streamStep` uses `fetch` + `ReadableStream` +
  `AbortController` (deliberately not `EventSource`, which would auto-reconnect and
  re-run a cancelled step; ADR 46). Admin token in localStorage.
- `derive.js` — mirrors `checks.py`: `lensRate`, `adjustedLensRate`, `subClaimRate`,
  `signedAdjustment`, `claimSupport`, `pct`, `domainOf`.
- `hooks/useStepStream.js` — one stream at a time; unmount aborts, abort cancels
  server-side. `hooks/useRuns.js` — the sidebar list.
- `App.jsx` — selection model (`new` | run id); backlog runs get `BacklogView`,
  everything else `RunView`; light/dark theme toggle; `/config`-driven chips.
- Components enforce the spec5 layout contract: stages stack vertically; `LiveTail` is
  used only in the decompose section; `LensCard` + `CellActivity` render headline-first
  cards with processing inside the active card; `SynthesisSection` shows the arithmetic
  table (counted → adjusted → weighted → chain → implied via `derive.js`), the final
  probability, rationale, and surviving violations; `StepControls` is the gate button
  (Run / Retry, with a deeper-budget retry on budget failures); `FieldEditor` +
  `NewForecastView`/`BacklogView` implement the visible four-field gate and the AI-draft
  flow.

---

## Database (SQLite, `SCHEMA_VERSION = 2`)

| Table | Purpose |
|---|---|
| `forecasts` | one forecast + resolution/scoring columns |
| `forecast_updates` | probability history (time-weighted scoring input) |
| `gated_runs` | one gated run: four question fields, `max_iterations`, `status`, nullable `error`, `forecast_id`, timestamps |
| `run_steps` | one row per stage cell: `stage`, `sub_claim_id`, `lens_name`, `status`, `payload_json`, `error`, `attempts`, timestamps; `UNIQUE(run_id, stage, sub_claim_id, lens_name)`; `ON DELETE CASCADE` |

Dropped by migration v2: `questions`, `votes`, `refresh_runs`, `runs`.

---

## Environment variables

All optional except one LLM key. Read via `backend/config.py`; `backend/.env` loaded with
`override=False` (real env wins; `superforecaster config` shows provenance).

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` / `PYDANTIC_AI_GATEWAY_API_KEY` | — | the model (one required; gateway wins) |
| `TAVILY_API_KEY` | — | web search; Wikipedia-only fallback without it |
| `AGENT_MODEL` | — | override model for every agent |
| `ADMIN_API_KEY` | — | required for admin routes off localhost; unset = local mode |
| `DATABASE_PATH` | `./superforecaster.db` | SQLite location |
| `FRONTEND_DIR` | `../frontend/dist` | static frontend served at `/` |
| `LOGFIRE_TOKEN` | — | cloud traces |
| `AGENT_TIMEOUT_SECONDS` | 180 | one agent run (0 disables) |
| `STAGE_TIMEOUT_SECONDS` | 600 | one gated step (0 disables) |
| `AGENT_MAX_TOKENS` | 16384 | output-token ceiling per model response — the provider default (4096) truncates the synthesize agent's Forecast mid-tool-call (`IncompleteToolCall`) |
| `AGENT_REQUEST_LIMIT` / `AGENT_TOOL_CALLS_LIMIT` | 40 / 20 | process-wide fallback limits |
| `CELL_SOFT_CALLS_PER_ITERATION` / `CELL_HARD_HEADROOM` | 1 / 3 | per-cell search budget |
| `CRITIQUE_SOFT_CALLS` / `CRITIQUE_HARD_HEADROOM` | 2 / 1 | critic budget |
| `MONITOR_TOOL_CALLS` | 4 | resolution / update / post-mortem agents |
| `RESEARCH_REQUESTS_PER_ITERATION` / `RESEARCH_TOOL_CALLS_PER_ITERATION` | 3 / 3 | whole-question fallback path |
| `REFRESH_CRON_SCHEDULE` | `0 6 * * *` | daily refresh |
| `MIN_PROBABILITY_DELTA` | 0.03 | below this an update is noise |
| `SEARCH_LOOKBACK_HOURS` | 48 | refresh search window |
| `CHECK_*` (11 vars) | see `get_check_thresholds` | tunable check thresholds, incl. `CHECK_DERIVATION_SLACK=0.05` (the ±5-point rule) |
| `MODEL_GARDEN_MARGIN_DAYS` | 90 | cutoff safety margin |

Removed in the rebuild: `DIGEST_CRON_SCHEDULE`, `RUN_MAX_CONCURRENT`,
`RUN_EVENT_BUFFER`, `RUN_RETENTION_MINUTES`, `RUN_TIMEOUT_SECONDS`,
`RUN_WATCHER_GRACE_SECONDS`, `DBOS_DATABASE_URL`.

---

## Tests (285, all passing)

| File | Covers |
|---|---|
| `test_machine.py` | materialization fan-out, gate enforcement, retry, cancel→`cancelled`, stage timeout, global one-slot, synthesis→forecast→complete |
| `test_db_gated_runs.py` | payload round-trips, claim CAS, red-chip mirror/clear, restart sweep, cascade delete |
| `test_api_gated_runs.py` | CRUD statuses (201/409/422/404), stream frame order, cancel-on-disconnect, busy 409 |
| `test_cli_autoadvance.py` | `run_all` ordering + degraded cells |
| `test_db_migrations.py` | v1 + v2 migration paths, fresh-db stamping |
| `gated_factories.py` | shared payload factories (not a test module) |
| `test_checks.py`, `test_component_scorers.py`, `test_config.py`, `test_critic_budget.py` (AST-enforced UsageLimits), `test_cron_orchestrators.py`, `test_db_forecasts.py`, `test_api_forecasts.py`, `test_fixtures.py`, `test_graph_update.py`, `test_model_garden.py`, `test_tools_backdating.py` | preserved suites |

Deleted with the code they tested: `test_runs`, `test_durability`, `test_graph_stream`,
`test_fanout`, `test_graph_forecast`, `test_db_runs`, `test_db_questions`,
`test_api_questions`, `test_api_runs`, `test_run_deadlines`.

---

## Dependencies (`backend/pyproject.toml`)

| Package | Why |
|---|---|
| `pydantic-ai` ≥ 0.4.0 | agent framework |
| `pydantic` ≥ 2.10.0 | all models |
| `fastapi` + `uvicorn[standard]` | API + server |
| `sse-starlette` ≥ 3.3.4 | SSE framing for the step stream |
| `anthropic` ≥ 0.40.0 | the model |
| `httpx` | Tavily / Wikipedia tools |
| `apscheduler` ≥ 3.10.0 | daily refresh |
| `logfire[system-metrics]` ≥ 4.25.0 | observability |
| `typer` ≥ 0.24.2 | the CLI |
| `python-dotenv`, `python-multipart` | env loading, form parsing |
| dev: `pytest`, `pytest-asyncio`, `freezegun` | tests |

Removed: `dbos`. Frontend: `react` 18, `react-dom` 18; dev `vite` 6,
`@vitejs/plugin-react` — four packages total (ADR 47).

---

## What actually works

- The full gated flow end to end: draft-from-text → backlog → four-field start gate →
  decompose → per-sub-question lenses → per-cell base rates → per-cell inside view →
  synthesis with the checks loop → saved forecast; every step user-gated, every payload
  persisted, reload-safe.
- Retry of any failed step (optionally with `?max_iterations=` up to 50); cancel by
  closing the tab; restart sweep marks orphans honestly.
- The CLI pipeline (`forecast`) and component evals through the same `stages` functions.
- Daily refresh cron + manual refresh through the update graph; resolution + scoring +
  calibration report.
- Local mode: two exported keys and `serve`, no token, no `.env`, no build step for the
  API (UI needs `npm run build` once).
- CI on push/PR; opt-in pre-push test gate.

## Known issues

- **Review gates are advance-only** — a decomposition or lens set cannot be edited before
  advancing; that needs payload-edit endpoints and downstream invalidation (deferred in
  spec5).
- **A permanently failing cell blocks its run** — strict gating has no skip-a-cell escape
  hatch; retry (optionally deeper) is the only recovery today (deferred in spec5).
- The end-to-end backtest (`test e2e`) is specified in `spec/planned/spec4.md` and not
  built; `superforecaster test e2e` says so and exits 2.

## Deployment assets

Nothing is hosted. `docker-compose.yml` runs the API with a named SQLite volume and the
bind-mounted `frontend/dist`; `.github/workflows/ci.yml` is the only automation.
