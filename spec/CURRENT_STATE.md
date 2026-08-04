# Current State

What exists in the codebase today, what works, and what's still missing.

---

## Repository Layout

```
.
├── backend/                          # All Python code
│   ├── config.py                     # Loads backend/.env; typed settings via get_settings()
│   ├── superforecaster/              # Core package
│   │   ├── __init__.py               # Imports config (loads backend/.env)
│   │   ├── models.py                 # All Pydantic models
│   │   ├── tools.py                  # Date-clamped search tools  (clamp 1)
│   │   ├── checks.py                 # Pure methodology validators
│   │   ├── deps.py                   # ForecastDeps — the two clamps + leak audit
│   │   ├── model_garden.py           # Model registry by training cutoff  (clamp 2)
│   │   ├── model_garden.json         # Registry data
│   │   ├── db.py                     # SQLite layer + scoring math
│   │   ├── cron.py                   # Schedulers + orchestrators
│   │   ├── observability.py          # Logfire config + run_agent wrapper
│   │   ├── __main__.py               # CLI
│   │   ├── agents/                   # 8 modules, one per methodology step
│   │   ├── graphs/                   # state.py, forecast.py, update.py
│   │   ├── evals/                    # components.py + components/*.json (empty)
│   │   └── fixtures/
│   ├── api/                          # FastAPI layer (flat)
│   │   ├── main.py                   # App + lifespan + CORS
│   │   ├── deps.py                   # require_admin, IP extraction + hashing
│   │   ├── forecasts.py
│   │   ├── questions.py
│   │   ├── calibration.py
│   │   └── admin.py
│   ├── tests/                        # 221 pytest tests
│   ├── pyproject.toml
│   ├── uv.lock
│   └── Dockerfile
├── frontend/                         # Next.js 15 + MUI v6 + TypeScript
│   ├── app/
│   │   ├── layout.tsx                # Root layout w/ MUI theme + nav header
│   │   ├── providers.tsx             # AppRouterCacheProvider, ThemeProvider, Dayjs adapter
│   │   ├── page.tsx                  # /            — Submit & Vote
│   │   ├── predictions/page.tsx      # /predictions — In-progress
│   │   ├── resolved/page.tsx         # /resolved    — Resolved + calibration
│   │   ├── forecasts/[id]/page.tsx   # /forecasts/[id] — Detail
│   │   └── admin/                    # /admin — 4-tab admin panel
│   │       ├── page.tsx
│   │       ├── PendingQuestionsTab.tsx
│   │       ├── ApprovedQuestionsTab.tsx
│   │       ├── MonthlyTopTab.tsx
│   │       ├── ForecastsAdminTab.tsx
│   │       └── ApproveDialog.tsx
│   ├── components/
│   │   ├── Header.tsx
│   │   ├── QuestionCard.tsx
│   │   ├── SubmitForm.tsx
│   │   ├── ForecastSummaryCard.tsx
│   │   ├── EditQuestionDialog.tsx
│   │   └── AdminLogin.tsx
│   ├── lib/
│   │   ├── api.ts                    # Typed fetch wrappers; types mirror backend models
│   │   └── utils.ts                  # date / percent / Brier-color helpers
│   ├── theme.ts                      # MUI theme (light + dark via colorSchemes)
│   ├── next.config.ts                # output: "standalone" for Docker
│   ├── package.json
│   ├── tsconfig.json
│   └── Dockerfile                    # Multi-stage Node.js standalone build
├── spec/
│   ├── TECHNICAL_DIRECTION.md
│   ├── CURRENT_STATE.md              # this file
│   ├── superforecasting_methodology.md
│   └── change_specs/
│       ├── SPEC_04_26_2026.md        # v3 — the five shipped specs
│       ├── SPEC_IN_PROGRESS.md       # v4 — agent decomposition + graphs (active)
│       └── spec4.md                  # end-to-end backtest (paused, needs a corpus)
├── docker-compose.yml                # api + frontend services
├── backend/.env.example
├── frontend/.env.example
├── .env.example                      # Pointer to split env files
├── .gitignore
├── CLAUDE.md
├── LICENSE
└── README.md
```

---

## Data Models (`superforecaster/models.py`)

All Pydantic v2 models in one file:

- **Graph step outputs**: `Decomposition`, `OutsideView`, `ReferenceClass`, `InsideView`, `Adjustment`, `BiasCheck`
- **Agent IO**: `Forecast`, `ForecastInput`, `UpdateDecision`, `UpdateOutcome`, `EvidenceItem`, `ResolutionCheckResult`, `CriteriaCritique`, `PostMortem`
- **Methodology checks**: `CheckViolation`
- **Decomposition / research**: `SubPrediction` (carries `knowability`), `HistoricalAnalog`, `ResearchSummary`
- **Contamination clamps**: `SourceRef` (leakage audit), `ModelEntry` (model garden)
- **Evals**: `GoldenQuestion`, `QuestionScore`, `Scorecard`, `ComponentCase`, `ComponentScore`, `ComponentReport`
- **DB records**: `ForecastRecord`, `ForecastUpdateRecord`, `QuestionRecord`
- **API request bodies**: `CreateForecastRequest`, `AddUpdateRequest`, `ResolveRequest`, `CreateQuestionRequest`, `EditQuestionRequest`, `CritiqueQuestionRequest`, `VoteRequest`, `ApproveQuestionRequest`
- **API responses**: `VoteResponse`, `RefreshActionResponse`, `RefreshSummary`, `CalibrationReport`, `CalibrationBucket`

Several constraints carry methodology weight rather than just validating shape:
`OutsideView.reference_classes` has `min_length=2` (principle 7), `InsideView.bias_checks`
is exactly 5 (principle 15), and `SubPrediction.knowability` defaults to `"judgment"` so
forecasts persisted before principle 2 existed still deserialize.

`GoldenQuestion`, `QuestionScore`, and `Scorecard` ship unused — they are the contract for
the end-to-end backtest deferred to `spec/change_specs/spec4.md`.

---

## Tools (`superforecaster/tools.py`)

Every tool takes `RunContext[ForecastDeps]` and reads `ctx.deps.as_of`. When it is set,
the tool must not return anything published after that date — this is clamp 1 of the two
contamination clamps. When it is `None` (production) the tools behave as before.

| Tool | Source | Behavior |
| --- | --- | --- |
| `search_web(ctx, query)` | Tavily API | Adds `end_date` + `topic="news"` when `as_of` is set; `_drop_leaked` then removes anything newer or undated |
| `search_wikipedia(ctx, topic)` | Wikipedia API | Fetches the article revision as it stood on `as_of` via `prop=revisions&rvstart=...&rvdir=older`; no key required |
| `find_disconfirming_evidence(ctx, claim)` | Tavily API | Principle 14 as a tool — runs three rewrites aimed at the opposite conclusion |

Pure helpers extracted so they can be asserted without a network call: `_tavily_body`,
`_wikipedia_params`, `_drop_leaked`, `_parse_published`, `_extract_page_text`.

Every URL an agent sees is recorded on `ForecastDeps.sources_seen`;
`ForecastDeps.leaked_sources` surfaces any dated after `as_of` and should always be empty.

---

## Methodology Checks (`superforecaster/checks.py`)

Pure functions over Pydantic models returning `CheckViolation | None`. No LLM, no network.
Thresholds come from `config.CheckThresholds` — there are no numeric literals in the module.

| Function | Principle | Catches |
| --- | --- | --- |
| `check_decomposition` | 1, 2 | no chain note, missing rationale, nothing labelled researchable |
| `check_dragonfly` | 7 | reference classes disagreeing materially with no explanation |
| `check_derivation` | 6 | final probability drifting from base rate + stated adjustments |
| `check_signal_vs_noise` | 9 | missing flip test, or evidence called noise that still moved the number |
| `check_disconfirming` | 14 | empty steel-man, or every adjustment pointing one way |
| `check_bias_coverage` | 15 | duplicate or unassessed biases |
| `check_calibration_hygiene` | 16 | unearned near-certainty |
| `check_bayes_direction` | 11 | probability moving against the agent's own likelihood ratios |
| `check_update_magnitude` | 10, 12 | under-reaction — real evidence, no movement |
| `is_large_move` | 12 | routing signal (not a violation) for the verification pass |
| `evidence_weight` | 11 | `SUM log(p_if_true / p_if_false)` — the total weight of evidence |

Suites: `run_forecast_checks`, `run_update_checks`, `blocking`.

There is deliberately **no** per-forecast granularity check. A forecast can legitimately
land on 0.60; principle 8 is a property of a distribution and belongs in a run-level
statistic, not a per-answer gate.

---

## Model Garden (`superforecaster/model_garden.py`)

Clamp 2: a model whose training cutoff predates the question cannot know the answer.
Cutoffs come from Anthropic's published docs — the *training data* cutoff, stored as the
last day of the stated month (both the conservative choice).

`pick_clean_model(as_of, margin_days=90)` returns the newest eligible entry, or `None`.
It never falls back to a contaminated model; the caller must skip.

Also: `load_garden`, `list_models`, `resolve_id` (adds the gateway prefix when routed
through the Pydantic AI Gateway), `earliest_cutoff`, `coverage`, `probe`, `probe_all`,
`render_garden`.

**Measured reach (2026-08-03):** earliest available training cutoff is **Jul 2025**
(Sonnet 4.5 / Haiku 4.5), so a question needs `asked_at >= 2025-10-29` to be clean-scorable.
The 66 legacy questions in `test_forecasting_baseline/` span Sep 2020 – Sep 2024 and give
`0/66` clean coverage, which is why the end-to-end backtest is deferred to `spec4.md`.

---

## Agents (`superforecaster/agents/`)

One module per methodology step. Each has the same four parts — `INSTRUCTIONS`,
`build_*_agent`, `get_*_agent` (lazy singleton, import-safe without API keys), and a
`run_*` entry point that graph nodes, tests, and evals all call.

| Module | Principles | `output_type` | Tools |
| --- | --- | --- | --- |
| `decompose.py` | 1, 2 | `Decomposition` | — |
| `outside_view.py` | 4, 7 | `OutsideView` | search_web, search_wikipedia |
| `inside_view.py` | 5, 9, 14, 15 | `InsideView` | search_web, search_wikipedia, find_disconfirming_evidence |
| `synthesize.py` | 6, 8, 16 | `Forecast` | — |
| `resolution.py` | — | `ResolutionCheckResult` | search_web, search_wikipedia |
| `update.py` | 10, 11, 12 | `UpdateDecision` | search_web, find_disconfirming_evidence |
| `critic.py` | 3 | `CriteriaCritique` | search_web |
| `postmortem.py` | 13 | `PostMortem` | search_web |

`critic.py` and `postmortem.py` are standalone — outside both graphs. `agents/__init__.py`
holds `with_model` (applies `deps.model` via `agent.override` for one run, so the model
garden can swap models per question), `format_question`, and `as_of_note`.

---

## Graphs (`superforecaster/graphs/`)

Orchestration only. Agents know nothing about each other.

**`forecast.py`** — `Decompose → FindBaseRates → AdjustInsideView → Synthesize → Critique`,
with `Critique` routing back to `Synthesize` once when a blocking violation is found.
`Critique` is pure — it runs `checks.run_forecast_checks` and makes a routing decision.
Entry point: `async run_forecast_graph(input, *, as_of, model, verbose) -> (Forecast, list[CheckViolation])`.
Surviving violations travel out with the result rather than being swallowed.

**`update.py`** — `CheckResolved` ends immediately when a forecast has already resolved,
so the probability update is unreachable for it. Otherwise `ApplyBayes → GuardUpdate`,
with `GuardUpdate` routing through `VerifyLargeMove` once when the jump exceeds
`CHECK_LARGE_MOVE` (default 0.75). A large move is corroborated, not capped.
Entry point: `async run_update_graph(forecast_id, *, verbose) -> UpdateOutcome`.
Replaces both `refresh_forecast(id)` and `check_resolution(id)`.

`forecast_mermaid()` / `update_mermaid()` render the real wiring, so the diagrams in the
specs cannot drift from the code.

**`state.py`** — `ForecastState`, `UpdateState`, and a re-export of `ForecastDeps`.
`ForecastDeps` itself lives in `superforecaster/deps.py` because `tools` needs it and
`graphs` imports `agents` which imports `tools`.

---

## Evals (`superforecaster/evals/`)

`components.py` holds the per-agent test harness and all eight scorers. Each scorer
returns named assertions rather than a bare pass/fail. Two encode a judgment worth
knowing about: `score_resolution` treats a false positive as fatal (closing a live
forecast is irreversible), and `score_postmortem` rewards calling a sound-but-missed
forecast `sound_process` — a scorer that penalised that would teach outcome bias.

`components/*.json` ship as `[]`. The scorers are the durable part; the cases are
researched content and filling them is data entry against scorers that already exist.

The end-to-end backtest (`runner.py`, `scoring.py`, golden question set) is **not built** —
see `spec/change_specs/spec4.md`.

---

## Database (`superforecaster/db.py`)

SQLite, file path from `DATABASE_PATH` env var (default `./superforecaster.db`).

**Tables:**

- `forecasts` — full schema per Spec 1: includes `submission_deadline`, `scored_probability`, `brier_score`, `last_refreshed_at`, `flagged_for_resolution_review`, `decompositions_json`, `research_json`
- `forecast_updates` — one row per probability estimate; FK to forecasts; cascade delete; `is_late` flag
- `questions` — community submissions; `ip_hash` (SHA-256), `resolution_criteria`, `proposed_resolution_date`, `edited_at`, `is_deleted`, `status`, `forecast_id` FK
- `votes` — `(question_id, ip_hash)` unique pair; `vote` constrained to ±1
- `refresh_runs` — history of daily refresh runs (timestamp + JSON summary)

**Custom datetime adapters** registered for round-tripping timezone-aware datetimes (Python 3.12 deprecation workaround).

**Functions** (full list):

- Forecasts: `save_forecast`, `add_forecast_update`, `get_forecast`, `list_forecasts`, `list_active_forecast_ids`, `compute_time_weighted_probability`, `resolve_forecast`, `mark_refreshed`
- Questions: `submit_question` (rate-limited), `edit_question` (IP-gated), `delete_question`, `get_question`, `list_questions`, `get_top_monthly`, `approve_question`, `reject_question`, `link_question_to_forecast`
- Votes: `cast_vote`, `remove_vote`, `get_vote`
- Calibration: `calibration_report` (decile buckets, excludes ambiguous)
- Refresh history: `record_refresh_run`, `last_refresh_run`
- Helpers: `hash_ip`, `init_db`, `connect`

**Custom exceptions:** `RateLimitError`, `NotFoundError`, `PermissionError`, `StateError`.

---

## Schedulers (`superforecaster/cron.py`)

APScheduler `AsyncIOScheduler`, started in FastAPI lifespan.

`**run_daily_refresh()`** — two-sweep orchestrator:

1. Resolution sweep: `check_resolution(fid)` for each active forecast
2. Probability sweep: `refresh_forecast(fid)` for each non-flagged active forecast
3. Records `RefreshSummary` in `refresh_runs` table

`**run_monthly_digest(n=5)**` — promotes top-voted pending questions to approved.

`**preview_monthly_digest(n=5)**` — same query, no mutation.

**Schedules** (configurable via env):

- `REFRESH_CRON_SCHEDULE` (default `0 6 * * `*) — daily 06:00 UTC
- `DIGEST_CRON_SCHEDULE` (default `0 9 28-31 * *`) — daily 09:00 on days 28-31; job checks if today is the actual last day of the month before running

---

## CLI (`superforecaster/__main__.py`)

`uv run python -m superforecaster <command>` from `backend/`.

| Command | Behavior |
| --- | --- |
| `forecast` | Prompts for question, criteria, source, date, category; runs the forecast graph; saves to DB. Prints any surviving methodology violations to stderr |
| `forecast --fixture [path]` | Same from a fixture file; `--no-save` skips the DB write |
| `refresh --id <uuid>` | Runs the full update graph on a DB forecast |
| `refresh --fixture` | Runs the update agent alone on a fixture record; no DB write |
| `resolve --id <uuid>` \| `--fixture` | Runs the resolution agent only |
| `critique --question ... --criteria ... [--date]` | Principle 3 — is this question resolvable as written? |
| `postmortem <uuid>` | Principle 13 — separate process errors from outcome noise |
| `models [list\|probe\|pick --as-of DATE]` | Inspect the model garden; `probe` marks what is still served and reports the reach |
| `diagram [forecast\|update]` | Print the real graph wiring as mermaid |
| `test component [agent\|all]` | Run the component eval harness (reports `0 cases` until data is added) |

`test e2e` exits 2 with a pointer to `spec/change_specs/spec4.md` — the backtest is not built.

All commands print formatted JSON to stdout.

---

## API (`api/`)

FastAPI app at `api.main:app`. Run with `uv run uvicorn api.main:app --reload` from `backend/`.

**Endpoints (27 routes total):**

Public:

- `GET /healthz` — Docker health check
- `GET /forecasts` — list (with `?status=active|resolved|ambiguous`)
- `GET /forecasts/{id}`
- `GET /questions` — list (with `?status=...&sort=score|newest`)
- `GET /questions/top-monthly` — top 5
- `GET /questions/{id}`
- `POST /questions` — IP rate-limited (1 per 24h)
- `PUT /questions/{id}` — IP-gated (or admin)
- `DELETE /questions/{id}` — IP-gated soft delete
- `POST /questions/{id}/vote` — body `{vote: ±1}`
- `DELETE /questions/{id}/vote` — undo
- `GET /calibration`

Admin (Bearer `ADMIN_API_KEY`):

- `POST /forecasts` — runs forecast agent, persists
- `POST /forecasts/{id}/updates`
- `PATCH /forecasts/{id}/resolve` — `outcome: 0.0|1.0|null`
- `POST /forecasts/{id}/refresh` — manual single-forecast refresh
- `POST /questions/{id}/approve` — body can override criteria + date
- `POST /questions/{id}/reject`
- `POST /questions/{id}/forecast` — runs agent on approved question, links result
- `GET /admin/digest/preview`
- `POST /admin/digest/run`
- `POST /admin/refresh/run`
- `GET /admin/refresh/status`

CORS open. Swagger UI at `/docs`.

---

## Frontend (`frontend/`)

Next.js 15 + MUI v6 + TypeScript, deployable to Vercel or via Docker.

### Stack

- **Next.js 15.5** with App Router (standalone output for Docker)
- **MUI v6** (`@mui/material`, `@mui/icons-material`, `@mui/x-date-pickers`)
- **Dayjs** for the DatePicker (chosen over date-fns because date-fns v3+ broke a deep import that MUI x-date-pickers v7 still uses)
- **TypeScript strict mode**

### Pages (5)


| Path              | Purpose                                                                                                                                                                                                                                                 |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/`               | Submit & Vote — list pending questions sorted by net score, top 5 highlighted with a colored MUI Card border + Star chip; sticky `<Paper>` footer with the 3-field submission form (text, criteria, date); inline edit & delete for own submissions     |
| `/predictions`    | In-progress forecasts ordered by soonest resolution; each row links to detail; `last_refreshed_at` shown as relative time                                                                                                                               |
| `/resolved`       | Resolved forecasts with YES/NO outcome chips; aggregate Brier score banner with calibration-by-bucket horizontal bars; per-row Brier color (green < 0.1, yellow < 0.2, red ≥ 0.2)                                                                       |
| `/forecasts/[id]` | Full detail: probability summary, resolution criteria, update timeline (with late chips), research panel (historical analogs table, empirical base rate, causal forces, supporting/contradicting evidence), decomposition accordions, initial reasoning |
| `/admin`          | 4-tab admin panel — Pending Questions (edit/approve/reject), Approved Questions (run forecast), Monthly Top (digest preview + run-now), Forecasts (refresh, resolve YES/NO/Ambiguous, flagged forecasts pinned to top with amber alert)                 |


### Auth flow for `/admin`

- On first visit, MUI `Dialog` prompts for the admin Bearer token
- Stored in `localStorage` under key `superforecaster_admin_token`
- All admin API calls in `lib/api.ts` automatically attach `Authorization: Bearer <token>`
- "Sign out" clears the token

### API client (`lib/api.ts`)

- All TypeScript types mirror `backend/superforecaster/models.py` (kept in sync manually)
- `apiFetch<T>` helper centralizes JSON encoding, admin token attachment, and error handling
- Throws `ApiError` with the backend's `detail` string preserved, so 429 / 403 / 409 can be handled meaningfully in the UI
- `getAdminToken()` / `setAdminToken()` for the localStorage management

---

## What Actually Works

**Backend:**

- All 221 tests pass with no network access: `cd backend && uv run pytest`
- All eight agent modules import and build without API keys (lazy construction)
- The forecast graph visits its nodes in methodology order, verified by test — principle 4 is enforced structurally, not by prompt
- `Critique` routes back to `Synthesize` exactly once on a blocking violation, then ends regardless
- `CheckResolved` short-circuits to `End` on a resolved forecast, so the probability update is unreachable for it
- `GuardUpdate` routes through `VerifyLargeMove` exactly once on a large jump, never twice
- Every `checks.py` validator has a passing and a failing case; thresholds are env-tunable and tested as such
- `_tavily_body` / `_wikipedia_params` carry their date parameters when `as_of` is set and omit them when it is not
- `pick_clean_model` returns `None` rather than a contaminated fallback when nothing qualifies
- All eight component scorers are tested, including the two that encode judgment (false-positive weighting, outcome-bias resistance)
- DB schema initializes on first connect; migrations are idempotent
- Time-weighted Brier score matches the spec example exactly
- Submission rate-limit, vote toggle, soft-delete, IP-gated edits all enforced
- Monthly digest correctly promotes top pending → approved, skips already-approved
- FastAPI app loads with 28 routes (incl. `POST /questions/critique`); admin auth and CORS configured
- CLI builds; `--help`, `models list`, `diagram`, and `test component all` all run

**Frontend:**

- `npx next build` passes; all 7 routes (5 pages + `_not-found` + dynamic `[id]`) compile clean
- TypeScript strict mode passes (`npx tsc --noEmit`)
- Standalone production build at ~245 kB First Load JS for the heaviest page (`/`)
- All pages render against a running FastAPI backend (manual smoke test pending)

**Full stack:**

- `docker compose up --build` brings up both `api` and `frontend` services
- API healthcheck on `/healthz` gates frontend startup via `depends_on: condition: service_healthy`

---

## Known Gaps / Not Yet Smoke-Tested

**Live agent runs:** All eight agents have working code paths but have not been exercised against a live model in this build session. Tests use stubs and in-memory fixtures throughout — a green `pytest` proves the plumbing is correct, not that the prompts are any good.

**No end-to-end backtest.** There is still no measured accuracy number. The harness is designed and both contamination clamps are built, but the corpus is missing: the 66 legacy questions give `0/66` clean coverage against a garden whose earliest cutoff is Jul 2025. Deferred to `spec/change_specs/spec4.md`.

**Component golden data is empty.** All eight scorers ship; all eight `components/*.json` are `[]`. Until they are filled, principles 3 and 13 have no real coverage — both are standalone agents the graph never exercises.

**`models probe` has not been run against the live API.** The garden's `available` flags are all `false`, so `pick_clean_model` returns `None` for everything until someone runs it.

**Live frontend ↔ backend integration:** Both halves build and pass their own tests, but a manual end-to-end smoke test (submit a question via the UI, vote on it, run a forecast, verify it appears on `/predictions`) hasn't been performed in this session.

**Logfire:** `configure_logfire()` (`backend/superforecaster/observability.py`) validates `LOGFIRE_TOKEN` synchronously (`GET /v1/info`) before enabling cloud export. If the token is missing or invalid, cloud tracing is disabled and agent progress (prompt, tool calls/results, reasoning, final result) prints in full to the local console instead — no 401 warning is surfaced. When a valid token is configured, cloud tracing is active and local printing stays terse (gated by `--verbose`). Currently configured with a working `LOGFIRE_TOKEN` and `PYDANTIC_AI_GATEWAY_API_KEY`.

---

## Dependencies (`backend/pyproject.toml`)


| Package             | Version  | Purpose              |
| ------------------- | -------- | -------------------- |
| `pydantic-ai`       | ≥0.4.0   | Agent framework      |
| `pydantic`          | ≥2.10.0  | Models               |
| `anthropic`         | ≥0.40.0  | Claude SDK           |
| `fastapi`           | ≥0.115.0 | API framework        |
| `uvicorn[standard]` | ≥0.32.0  | ASGI server          |
| `httpx`             | ≥0.27.0  | Tool HTTP calls      |
| `apscheduler`       | ≥3.10.0  | Cron jobs            |
| `python-dotenv`     | ≥1.0.0   | `.env` loading       |
| `python-multipart`  | ≥0.0.20  | FastAPI form parsing |
| `logfire`           | ≥4.25.0  | Observability        |


Dev: `pytest`, `pytest-asyncio`, `freezegun`.

---

## Environment Variables

Backend settings live in `backend/.env` (see `backend/.env.example`). Frontend settings live in `frontend/.env` (see `frontend/.env.example`). All backend code reads env through `backend/config.py` (`get_settings()`).

| Variable                      | Purpose                       | Default                                |
| ----------------------------- | ----------------------------- | -------------------------------------- |
| `ADMIN_API_KEY`               | Bearer token for admin routes | — (required)                           |
| `PYDANTIC_AI_GATEWAY_API_KEY` | Logfire Gateway (`pylf_v...`) | — (required unless `ANTHROPIC_API_KEY` set) |
| `ANTHROPIC_API_KEY`             | Direct Anthropic API          | — (alternative to gateway)                 |
| `AGENT_MODEL`                   | Override model for all agents | `gateway/anthropic:claude-sonnet-4-6` or `anthropic:claude-sonnet-4-6` |
| `AGENT_REQUEST_LIMIT`           | Max LLM requests per agent run | `40` |
| `AGENT_TOOL_CALLS_LIMIT`        | Max tool calls per agent run   | `20` |
| `TAVILY_API_KEY`              | Web search                    | — (optional; tools degrade gracefully) |
| `LOGFIRE_TOKEN`               | Observability                 | — (optional)                           |
| `DATABASE_PATH`               | SQLite file                   | `./superforecaster.db`                 |
| `REFRESH_CRON_SCHEDULE`       | Daily update schedule         | `0 6 * * *`                            |
| `DIGEST_CRON_SCHEDULE`        | Monthly digest schedule       | `0 9 28-31 * *`                        |
| `MIN_PROBABILITY_DELTA`       | Update write threshold        | `0.03`                                 |
| `SEARCH_LOOKBACK_HOURS`       | Update news window            | `48`                                   |

Research budget per searching agent: `max_iterations × 2 + 1` requests, `max_iterations × 2`
tool calls (CLI default `max_iterations=5`). Synthesis: 4 requests, 0 tool calls.

Methodology check thresholds — all optional, all read fresh on every call so tests can
monkeypatch them:

| Variable | Purpose | Default |
| --- | --- | --- |
| `CHECK_RC_DISAGREEMENT` | P7 — reference-class spread that demands an explanation | `0.20` |
| `CHECK_RC_AGREEMENT` | P16 — spread under which classes count as agreeing | `0.10` |
| `CHECK_CALIBRATION_FLOOR` | P16 — lowest unearned probability | `0.02` |
| `CHECK_CALIBRATION_CEILING` | P16 — highest unearned probability | `0.98` |
| `CHECK_LARGE_MOVE` | P12 — jump that triggers `VerifyLargeMove` | `0.75` |
| `CHECK_DERIVATION_SLACK` | P6 — stated vs implied probability tolerance | `0.05` |
| `CHECK_ROUND_NUMBER_RATE` | P8 — run-level rounding rate that gets flagged | `0.40` |
| `MODEL_GARDEN_MARGIN_DAYS` | Safety margin on published training cutoffs | `90` |

Frontend:

| Variable               | Purpose    | Default                  |
| ---------------------- | ---------- | ------------------------ |
| `NEXT_PUBLIC_API_URL`  | API base   | `http://localhost:8000`  |


---

## Deployment Assets

- `backend/Dockerfile` — slim Python 3.12 + uv; copies `superforecaster/` and `api/`; runs uvicorn on `:8000`
- `frontend/Dockerfile` — multi-stage Node.js 20 standalone build; final image is small (only `.next/standalone` + `public` + `.next/static`); runs `node server.js` on `:3000`
- `docker-compose.yml` — both `api` and `frontend` services with `sqlite_data` named volume; frontend depends on api healthcheck
- `.env.example` at repo root — points to `backend/.env.example` and `frontend/.env.example`

`docker compose up --build` brings up the full stack: API on `:8000`, frontend on `:3000`.

---

## How to Run

### Tests

```bash
cd backend
uv run pytest
```

### CLI (single agent runs)

```bash
cd backend
uv run python -m superforecaster forecast --fixture
uv run python -m superforecaster refresh --fixture
uv run python -m superforecaster resolve --fixture
```

### API (local)

```bash
cd backend
uv run uvicorn api.main:app --reload
# Swagger: http://localhost:8000/docs
```

### Frontend (local)

```bash
cd frontend
npm install
npm run dev                # → http://localhost:3000
```

### Full stack (Docker)

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
docker compose up --build
# api:      http://localhost:8000  (Swagger at /docs)
# frontend: http://localhost:3000
```

### Verification suite

```bash
cd backend && uv run pytest                     # 62 tests
cd frontend && npx next build && npx tsc --noEmit  # build + typecheck
docker compose up --build -d                     # full stack starts
```

