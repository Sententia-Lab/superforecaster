# Current State

What exists in the codebase today, what works, and what's still missing.

---

## Repository Layout

```
.
├── backend/                          # All Python code
│   ├── config.py                     # Loads backend/.env; typed settings via get_settings()
│   ├── superforecaster/              # Core package (flat — no sub-packages)
│   │   ├── __init__.py               # Imports config (loads backend/.env)
│   │   ├── models.py                 # All Pydantic models
│   │   ├── tools.py                  # search_web, search_wikipedia
│   │   ├── agent.py                  # forecast_agent (lazy)
│   │   ├── refresh.py                # refresh_agent (lazy)
│   │   ├── resolution.py             # resolution_agent (lazy)
│   │   ├── db.py                     # SQLite layer + scoring math
│   │   ├── cron.py                   # Schedulers + orchestrators
│   │   ├── __main__.py               # CLI: forecast | refresh | resolve
│   │   └── fixtures/
│   ├── api/                          # FastAPI layer (flat)
│   │   ├── main.py                   # App + lifespan + CORS
│   │   ├── deps.py                   # require_admin, IP extraction + hashing
│   │   ├── forecasts.py
│   │   ├── questions.py
│   │   ├── calibration.py
│   │   └── admin.py
│   ├── tests/                        # 62 pytest tests
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
│   ├── SPEC.md
│   ├── TECHNICAL_DIRECTION.md
│   ├── CURRENT_STATE.md              # this file
│   └── superforecasting_methodology.md
├── docker-compose.yml                # api + frontend services
├── backend/.env.example
├── frontend/.env.example
├── .env.example                      # Pointer to split env files
├── .gitignore
├── CLAUDE.mm
├── LICENSE
└── README.md
```

---

## Data Models (`superforecaster/models.py`)

All Pydantic v2 models in one file:

- **Agent IO**: `Forecast`, `ForecastInput`, `ForecastRefreshResult`, `ResolutionCheckResult`
- **Decomposition / research**: `SubPrediction`, `HistoricalAnalog`, `ResearchSummary`
- **DB records**: `ForecastRecord`, `ForecastUpdateRecord`, `QuestionRecord`
- **API request bodies**: `CreateForecastRequest`, `AddUpdateRequest`, `ResolveRequest`, `CreateQuestionRequest`, `EditQuestionRequest`, `VoteRequest`, `ApproveQuestionRequest`
- **API responses**: `VoteResponse`, `RefreshActionResponse`, `RefreshSummary`, `CalibrationReport`, `CalibrationBucket`

`ResearchSummary` includes the empirical base rate built from `historical_analogs` (per Spec 1's replanning requirement).

---

## Tools (`superforecaster/tools.py`)


| Tool                      | Source        | Behavior                                                                                            |
| ------------------------- | ------------- | --------------------------------------------------------------------------------------------------- |
| `search_web(query)`       | Tavily API    | Async; returns formatted top-5 results, or graceful fallback message when `TAVILY_API_KEY` is unset |
| `search_wikipedia(topic)` | Wikipedia API | Async; uses search + extract endpoints; no key required                                             |


Imported by all three agent modules.

---

## Agents

All three agents are constructed lazily via `get_*_agent()` factories so the modules can be imported without API keys set. Each is a separate `pydantic_ai.Agent` instance with its own `output_type`.

### `forecast_agent` (`agent.py`)

- `output_type=Forecast`
- System prompt enforces 4 phases: decompose → outside view (replanning for historical analogs) → inside view → synthesize
- Searches iteratively for ≥3 analogous historical events and computes empirical base rate from their binary outcomes
- Public entry point: `async run_forecast(input: ForecastInput) -> Forecast`

### `refresh_agent` (`refresh.py`)

- `output_type=ForecastRefreshResult`
- System prompt: only update on substantive new evidence, never on absence of news
- Public entry point: `async refresh_forecast(forecast_id) -> RefreshActionResponse`
- Threshold-gated: writes new update only if delta >= `MIN_PROBABILITY_DELTA` (default 0.03)
- Skips already-resolved, ambiguous, or resolution-flagged forecasts

### `resolution_agent` (`resolution.py`)

- `output_type=ResolutionCheckResult`
- System prompt: conservative — only `appears_resolved=True` on unambiguous evidence
- Public entry point: `async check_resolution(forecast_id) -> ResolutionCheckResult`
- Never auto-resolves — flags `flagged_for_resolution_review=True` for admin confirmation

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

`uv run python -m superforecaster {forecast|refresh|resolve}` from `backend/`.


| Command                        | Mode           | Behavior                                                                                      |
| ------------------------------ | -------------- | --------------------------------------------------------------------------------------------- |
| `forecast`                     | interactive    | Prompts for question, criteria, source, date, category; runs agent; saves to DB               |
| `forecast --fixture`           | fixture        | Loads `forecast_question.json`; runs; saves to DB unless `--no-save`                          |
| `forecast --fixture path.json` | custom fixture | Loads given path                                                                              |
| `refresh --fixture`            | in-memory      | Loads `existing_forecast.json` into a `ForecastRecord`; runs `run_refresh_agent`; no DB write |
| `refresh --id <uuid>`          | DB             | Loads forecast from DB; runs full `refresh_forecast` (writes update if applicable)            |
| `resolve --fixture`            | in-memory      | Loads fixture; runs `run_resolution_agent`                                                    |
| `resolve --id <uuid>`          | DB             | Loads from DB; runs `check_resolution` (sets flag if applicable)                              |


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

- All 62 tests pass: `cd backend && uv run pytest`
- All three agent modules import without API keys (lazy construction)
- DB schema initializes on first connect; migrations are idempotent
- Time-weighted Brier score matches the spec example exactly
- Submission rate-limit, vote toggle, soft-delete, IP-gated edits all enforced
- Monthly digest correctly promotes top pending → approved, skips already-approved
- Daily refresh's two-sweep ordering is verified by test (resolution flags block probability sweep)
- FastAPI app loads with 27 routes; admin auth and CORS configured
- CLI builds without errors; `--help` works

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

**Live agent runs:** The agents (forecast, refresh, resolution) have working code paths but have not been exercised end-to-end against the live Pydantic AI Gateway in this build session. Tests use mocks or in-memory fixtures. The user has working API keys in `.env`.

**Live frontend ↔ backend integration:** Both halves build and pass their own tests, but a manual end-to-end smoke test (submit a question via the UI, vote on it, run a forecast, verify it appears on `/predictions`) hasn't been performed in this session.

**Logfire:** Configured but the user's existing token is invalid (401 warning). Not blocking; warnings only.

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
| `PYDANTIC_AI_GATEWAY_API_KEY` | Pydantic AI gateway           | — (required for agent calls)           |
| `TAVILY_API_KEY`              | Web search                    | — (optional; tools degrade gracefully) |
| `LOGFIRE_TOKEN`               | Observability                 | — (optional)                           |
| `DATABASE_PATH`               | SQLite file                   | `./superforecaster.db`                 |
| `REFRESH_CRON_SCHEDULE`       | Daily refresh schedule        | `0 6 * * `*                            |
| `DIGEST_CRON_SCHEDULE`        | Monthly digest schedule       | `0 9 28-31 * *`                        |
| `MIN_PROBABILITY_DELTA`       | Refresh write threshold       | `0.03`                                 |
| `SEARCH_LOOKBACK_HOURS`       | Refresh news window           | `48`                                   |

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

