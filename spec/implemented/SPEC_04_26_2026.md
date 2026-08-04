# Superforecaster v3 — Crowd-Sourced Forecasting Platform

A public platform where the community submits and upvotes forecast questions, the admin vets and triggers the AI forecasting agent, and results are displayed with calibration tracking.

---

## Scoring Model

This section is the source of truth for how forecasts are scored. All DB schema, API, and UI decisions downstream must be consistent with these rules.

### Time-Weighted Brier Score (Tetlock's Method)

A forecast may be updated multiple times before the resolution deadline. The scored probability is **not** the final update — it is the time-weighted average of all updates, where each update's weight equals the fraction of the total horizon it was held.

**Formula:**

```
scored_probability = Σ(probability_i × duration_i) / total_duration

brier_score = (scored_probability - outcome)²
```

**Example:**

- Day 0: forecast 30%, held 60 days
- Day 60: updated to 50%, held 30 days
- Resolution: Day 90

```
scored_probability = (0.30 × 60 + 0.50 × 30) / 90 = 36.7%
brier_score = (0.367 - outcome)²
```

This rewards accuracy over the full horizon and prevents a lucky last-minute update from erasing months of poor forecasting.

### Deadline Rules


| Rule                  | Value                                                    |
| --------------------- | -------------------------------------------------------- |
| Submission deadline   | `resolution_date - submission_gap_days`                  |
| `submission_gap_days` | Configurable per forecast category; default 7            |
| Update deadline       | `resolution_date` (strict — no updates after this)       |
| Late update flag      | Any update submitted within 24h of `resolution_date`     |
| Ambiguous resolution  | Forecast excluded from all scoring; `outcome` stays null |


**Why a configurable `submission_gap_days`?** The right gap differs by domain — a geopolitical question may need 30 days of lockout to prevent hindsight; a company earnings question may only need 1. Hardcoding 7 days would be wrong for both.

### Scoring Exclusions

A forecast is excluded from aggregate calibration stats (Brier score, bucket analysis) if:

- `is_ambiguous = true` — resolution criteria were unclear or the event did not cleanly resolve
- The forecast was submitted after the submission deadline (it should be rejected at the API layer, but if it somehow exists, exclude it)

Late-flagged forecasts are **included** in scoring — they are penalized implicitly because the bad early probability dominates the time-weighted average. The flag is informational only.

---

## Verification (all phases)

```bash
cd backend && uv run pytest     # Python backend + agent
cd frontend && npx next build   # Next.js frontend compiles without errors
docker compose up --build -d    # Full stack starts; api healthy on :8000, frontend on :3000
```

---

## Infrastructure: Docker

The full stack runs via `docker compose`. Required files live at the repo root alongside `pyproject.toml`.

### Files

The repo root layout after all specs are implemented:
```
backend/
  superforecaster/
    fixtures/
      forecast_question.json
      existing_forecast.json
      likely_resolved_forecast.json
    __init__.py
    models.py
    tools.py
    agent.py
    refresh.py
    resolution.py
    db.py
    cron.py
    __main__.py
  api/
    main.py
    forecasts.py
    questions.py
    calibration.py
    admin.py
    deps.py
  pyproject.toml
  uv.lock
  Dockerfile

frontend/
  app/
    page.tsx
    predictions/
    resolved/
    forecasts/[id]/
    admin/
  components/
  lib/
  next.config.ts
  package.json
  Dockerfile

docker-compose.yml    ← repo root
.env.example          ← repo root
```

**`backend/Dockerfile`**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev
COPY superforecaster/ ./superforecaster/
COPY api/ ./api/
CMD ["uv", "run", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**`frontend/Dockerfile`**
```dockerfile
FROM node:20-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/static ./.next/static
CMD ["node", "server.js"]
```

**`docker-compose.yml`** (repo root)
```yaml
services:
  api:
    build:
      context: ./backend
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    volumes:
      - sqlite_data:/app/data
    env_file: .env
    environment:
      DATABASE_PATH: /app/data/superforecaster.db
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/healthz"]
      interval: 10s
      timeout: 5s
      retries: 3

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "3000:3000"
    environment:
      NEXT_PUBLIC_API_URL: http://localhost:8000
    depends_on:
      api:
        condition: service_healthy

volumes:
  sqlite_data:
```

**`GET /healthz`** — add a health check endpoint to `api/main.py` that returns `{ status: "ok" }`. Required for Docker's health check.

### Environment

`.env.example` at repo root — copy to `.env` at repo root for Docker use. For running the backend locally without Docker, also copy to `backend/.env`.

```
# Backend (API + agents)
ADMIN_API_KEY=changeme
PYDANTIC_AI_GATEWAY_API_KEY=
TAVILY_API_KEY=
LOGFIRE_TOKEN=
REFRESH_CRON_SCHEDULE=0 6 * * *
MIN_PROBABILITY_DELTA=0.03
SEARCH_LOOKBACK_HOURS=48

# Frontend (build-time / server-side)
NEXT_PUBLIC_API_URL=http://localhost:8000
ADMIN_API_KEY=changeme
```

### Usage

```bash
# Docker (full stack)
cp .env.example .env
docker compose up --build     # first run
docker compose up             # subsequent runs
docker compose down -v        # tear down including data volume

# Local backend dev (no Docker)
cp .env.example backend/.env
cd backend
uv sync
uv run uvicorn api.main:app --reload

# Local frontend dev (no Docker)
cd frontend
npm install
npm run dev
```

### Notes
- SQLite database lives in the `sqlite_data` named volume when using Docker; locally it writes to `backend/superforecaster.db`
- The APScheduler (monthly digest + daily refresh) runs inside the `api` container — no separate worker container needed
- Next.js runs in standalone output mode (`output: 'standalone'` in `next.config.ts`) to minimize image size
- `pytest` is run from `backend/`: `cd backend && uv run pytest`

---

## Spec 1: Agent v3 Core

Rewrite the forecasting agent so the LLM output is actually parsed and used (v2 ignores it), and add SQLite persistence for forecast history and calibration tracking.

### Requirements

**Package structure**

Replace `superforecaster_v2.py` with a structured repo under `backend/`:
```
backend/
  superforecaster/
    __init__.py
    models.py         # All Pydantic models shared across agent, db, api
    tools.py          # search_web(), search_wikipedia() — imported by all agents
    agent.py          # forecast_agent    (output_type=Forecast)
    refresh.py        # refresh_agent     (output_type=ForecastRefreshResult)
    resolution.py     # resolution_agent  (output_type=ResolutionCheckResult)
    db.py             # All DB operations
    cron.py           # Scheduled jobs
    __main__.py       # CLI entry point
    fixtures/
      forecast_question.json
      existing_forecast.json
      likely_resolved_forecast.json
  api/
    main.py
    forecasts.py
    questions.py
    calibration.py
    admin.py
    deps.py
  pyproject.toml
  uv.lock
```

**Agent (`superforecaster/agent.py`)**

- Model: `claude-sonnet-4-6`
- `output_type=Forecast`; the agent returns a fully-populated `Forecast` in a single structured call — no separate prompts, no hardcoded samples
- `Forecast.decompositions`: 3–5 `SubPrediction` items generated by the LLM
- `Forecast.research`: a `ResearchSummary` populated after the agent calls `search_web` and `search_wikipedia`
- `combine_probabilities` and `calibrate_confidence` run as post-processing after the agent returns, not inside the agent

**Agent replanning and historical base rate search**

The agent must use iterative tool calls (replanning) with a configurable `max_iterations` (default 5). The purpose of replanning is specifically for building a credible base rate:

1. The agent first searches for the historical reference class: events similar to the question that have already resolved with a binary outcome (yes/no)
2. For each analogous historical event found, the agent records: description, outcome (0 or 1), and relevance to the current question
3. After collecting ≥ 3 analogous outcomes (or exhausting `max_iterations`), the agent computes the empirical base rate: `mean(outcomes)` of the collected historical events
4. This empirical base rate anchors the outside view before any inside-view adjustment

`ForecastInput` model (what the agent receives):
```
question:              str
resolution_criteria:   str
resolution_date:       datetime
category:              str
max_iterations:        int  — default 5
```

`HistoricalAnalog` model (intermediate, collected during replanning):
```
description:  str    — brief description of the analogous event
outcome:      float  — 0.0 or 1.0
relevance:    str    — why this analog applies to the current question
```

`ResearchSummary` model updated to include:
```
historical_analogs:  list[HistoricalAnalog]  — the events used to build the base rate
empirical_base_rate: float | None            — mean(analog outcomes); None if < 3 analogs found
base_rate_note:      str                     — caveat on base rate quality/applicability
causal_forces:       list[str]
evidence:            dict  — { "supporting": [...], "contradicting": [...] }
uncertainties:       list[str]
```

**SQLite persistence (`superforecaster/db.py`)**

On first run, create `superforecaster.db` with three tables:

`forecasts` — one row per forecast question:

```
id                    UUID, primary key
question              text
resolution_criteria   text  — exact observable event that counts as "yes"
resolution_source     text  — where the outcome will be verified
category              text  — e.g. "geopolitical", "economics", "tech"
submission_gap_days   int   — default 7; configurable per forecast
submission_deadline   datetime  — computed: resolution_date - submission_gap_days
resolution_date       datetime  — when the event resolves
resolved_at           datetime, nullable  — when admin recorded the outcome
outcome               float, nullable  — 0.0 (no) | 1.0 (yes) | null (unresolved/ambiguous)
is_ambiguous          bool  — if true, excluded from all scoring
scored_probability    float, nullable  — time-weighted average; computed at resolution
brier_score           float, nullable  — (scored_probability - outcome)² at resolution
last_refreshed_at                datetime, nullable  — when the daily refresh job last ran on this forecast
flagged_for_resolution_review    bool, default false — resolution_agent believes this has resolved; awaiting admin confirmation
initial_reasoning     text  — agent's reasoning from the first forecast run
decompositions_json   text  — agent's decomposition from first run
research_json         text  — agent's research from first run
created_at            datetime
```

`forecast_updates` — one row per probability estimate (including the initial one):

```
id              UUID, primary key
forecast_id     UUID, FK → forecasts.id
probability     float  — 0.0–1.0
confidence      text   — "low" | "medium" | "high"
reasoning       text   — why this update was made
is_late         bool   — true if submitted within 24h of resolution_date
created_at      datetime
```

`questions` — community-submitted forecast question ideas:

```
id                       UUID, primary key
text                     text
resolution_criteria      text      — user-defined; exact observable condition that constitutes "yes" or "no"
proposed_resolution_date datetime  — user-proposed; admin can override when approving
ip_hash                  text      — SHA-256 of submitter IP; never stored in plaintext
edited_at                datetime, nullable
is_deleted               bool, default false
status                   text  — "pending" | "approved" | "rejected" | "forecasted"
created_at               datetime
approved_at              datetime, nullable
forecast_id              UUID, nullable FK → forecasts.id
```

`votes` — one row per (question, IP) pair; UNIQUE constraint on `(question_id, ip_hash)`:

```
id           UUID, primary key
question_id  UUID, FK → questions.id
ip_hash      text   — SHA-256 of voter IP
vote         int    — +1 (upvote) or -1 (downvote)
created_at   datetime
updated_at   datetime
```

Net score for a question = `SUM(vote)` from the `votes` table. Questions are sorted by net score descending.

**DB functions:**

Forecast:
- `save_forecast(forecast: Forecast, resolution_date: datetime, resolution_criteria: str, resolution_source: str, category: str, submission_gap_days: int = 7) -> str`
- `add_forecast_update(forecast_id: str, probability: float, confidence: str, reasoning: str) -> str` — raises if after `resolution_date`; sets `is_late` if within 24h
- `get_forecast(id: str) -> ForecastRecord | None` — includes all updates ordered by `created_at`
- `list_forecasts(status: str | None, limit: int, offset: int) -> list[ForecastRecord]`
- `resolve_forecast(id: str, outcome: float | None)` — None → `is_ambiguous = true`; otherwise computes `scored_probability` + `brier_score`
- `compute_time_weighted_probability(forecast_id: str) -> float` — pure function

Questions:
- `submit_question(text: str, resolution_criteria: str, proposed_resolution_date: datetime, ip_hash: str) -> QuestionRecord` — raises `RateLimitError` if same `ip_hash` has a non-deleted submission in the last 24h
- `edit_question(id: str, text: str | None, resolution_criteria: str | None, ip_hash: str) -> QuestionRecord` — updates whichever fields are provided; raises if question is not `pending` or if `ip_hash` doesn't match original submitter
- `delete_question(id: str, ip_hash: str)` — soft delete (`is_deleted = true`); raises if `ip_hash` doesn't match or question is not `pending`
- `get_question(id: str) -> QuestionRecord | None`
- `list_questions(status: str | None, sort: str, limit: int, offset: int) -> list[QuestionRecord]` — excludes `is_deleted = true`; sort by `net_score` or `created_at`
- `get_top_monthly(n: int = 5) -> list[QuestionRecord]` — top N by net score among `pending`/`approved` questions submitted in current calendar month
- `cast_vote(question_id: str, ip_hash: str, vote: int) -> int` — upserts into `votes`; `vote` must be +1 or -1; returns new net score
- `remove_vote(question_id: str, ip_hash: str) -> int` — deletes vote row; returns new net score
- `get_vote(question_id: str, ip_hash: str) -> int | None` — returns +1, -1, or None

Calibration:
- `calibration_report() -> CalibrationReport` — only resolved, non-ambiguous forecasts; buckets `scored_probability` by decile; returns predicted vs actual per bucket + aggregate Brier score

**CLI (`superforecaster/__main__.py`)**

Three subcommands, one per agent. All support `--fixture` to load from a JSON fixture file instead of prompting. Results are printed as formatted JSON to stdout.

All CLI commands run from the `backend/` directory:

```bash
cd backend

# Forecast agent — produce an initial forecast
uv run python -m superforecaster forecast                        # interactive: prompts for question, criteria, date, category
uv run python -m superforecaster forecast --fixture              # loads superforecaster/fixtures/forecast_question.json
uv run python -m superforecaster forecast --fixture path/to.json # custom fixture file

# Refresh agent — check for probability update on an existing forecast
uv run python -m superforecaster refresh --fixture               # loads superforecaster/fixtures/existing_forecast.json
uv run python -m superforecaster refresh --id <uuid>             # loads forecast from superforecaster.db

# Resolution agent — check if a forecast has resolved
uv run python -m superforecaster resolve --fixture               # loads superforecaster/fixtures/existing_forecast.json
uv run python -m superforecaster resolve --id <uuid>             # loads forecast from superforecaster.db
```

Interactive mode (no `--fixture`, no `--id`) only applies to `forecast`. `refresh` and `resolve` require either `--fixture` or `--id` — they need an existing forecast to operate on.

---

**Test Fixtures (`fixtures/`)**

All fixture files live at the repo root in `fixtures/`. They contain realistic, well-specified questions that produce meaningful agent output.

`fixtures/forecast_question.json` — input for the forecast agent:
```json
{
  "question": "Will the US impose new tariffs specifically targeting Chinese semiconductor imports before December 31, 2026?",
  "resolution_criteria": "The US government (via Executive Order, USTR action, or Congressional legislation signed into law) imposes import tariffs or duties that specifically apply to semiconductors or chipmaking equipment originating from China, beyond any measures already in effect as of January 1, 2026. Confirmed by publication in the Federal Register or official White House/USTR press release.",
  "resolution_source": "Federal Register (federalregister.gov), USTR official statements, White House press releases",
  "resolution_date": "2026-12-31",
  "category": "economics"
}
```

`fixtures/existing_forecast.json` — input for the refresh and resolution agents; represents a forecast mid-life with two prior updates:
```json
{
  "id": "fixture-forecast-001",
  "question": "Will the Federal Reserve cut the federal funds rate at least twice before December 31, 2026?",
  "resolution_criteria": "The FOMC votes to reduce the federal funds rate target range at two or more separate scheduled meetings between January 1, 2026 and December 31, 2026 (inclusive). Each cut must be a separate meeting decision, not a single large cut. Confirmed by official FOMC post-meeting statements at federalreserve.gov.",
  "resolution_source": "Federal Reserve official FOMC statements at federalreserve.gov",
  "resolution_date": "2026-12-31",
  "category": "economics",
  "submission_gap_days": 7,
  "updates": [
    {
      "probability": 0.71,
      "confidence": "medium",
      "reasoning": "CME FedWatch pricing in ~70% probability of two cuts. Labor market softening but inflation still above 2% target. Historical cutting cycles suggest Fed moves slowly once it starts.",
      "created_at": "2026-01-20T09:00:00Z"
    },
    {
      "probability": 0.64,
      "confidence": "medium",
      "reasoning": "January CPI came in at 3.1%, above the 2.9% consensus. Reduces probability the Fed will move quickly. Still likely they cut at least once; second cut now less certain.",
      "created_at": "2026-02-14T09:00:00Z"
    }
  ]
}
```

`fixtures/likely_resolved_forecast.json` — a forecast whose underlying event has plausibly already occurred; used specifically to stress-test the resolution agent:
```json
{
  "id": "fixture-forecast-002",
  "question": "Will Elon Musk step down or be removed from his role leading the Department of Government Efficiency (DOGE) before April 30, 2026?",
  "resolution_criteria": "Elon Musk is no longer serving in any official or advisory leadership capacity at DOGE (or its functional successor), as confirmed by a White House official statement, Musk's own public statement, or reporting from two independent major news outlets (NYT, WSJ, WaPo, Reuters, or AP).",
  "resolution_source": "White House press releases, Musk public statements, major news outlets",
  "resolution_date": "2026-04-30",
  "category": "politics",
  "submission_gap_days": 14,
  "updates": [
    {
      "probability": 0.38,
      "confidence": "low",
      "reasoning": "High uncertainty. Musk's relationship with the administration appears strong but his time commitments and public controversies create meaningful probability of departure.",
      "created_at": "2026-01-10T09:00:00Z"
    }
  ]
}
```

The fixture files are checked into the repo. They are not used by `pytest` directly — they exist for manual agent testing via the CLI.

### Success Criteria

- `uv run pytest` passes
- `uv run python -m superforecaster forecast --fixture` completes and prints a `Forecast` JSON object with LLM-generated decompositions and a populated `research.historical_analogs` list
- `uv run python -m superforecaster refresh --fixture` completes and prints a `ForecastRefreshResult` JSON object
- `cd backend && uv run python -m superforecaster resolve --fixture superforecaster/fixtures/likely_resolved_forecast.json` completes and prints a `ResolutionCheckResult` JSON object — the resolution agent should find evidence and return `appears_resolved: true` with high probability given the question's date
- All three commands exit with code 0 on success, non-zero on API key errors or network failures
- A forecast run saves to `superforecaster.db`; a second run with `--id <uuid>` loads it correctly for refresh/resolve

### Ralph Command

```
/ralph-loop:ralph-loop "Read /Users/narenchaudhry/personal/superforecaster/SPEC.md and implement Spec 1: Agent v3 Core" --max-iterations 30 --completion-promise "uv run pytest passes and the CLI produces real LLM decompositions saved to SQLite"
```

---

## Spec 2: FastAPI REST API

Add a FastAPI backend exposing the agent and community question system over HTTP, with admin actions protected by API key.

### Prerequisites

- Spec 1 complete

### Requirements

**App structure (flat, lives under `backend/`)**

```
backend/
  api/
    main.py         # FastAPI app + lifespan (schedulers)
    forecasts.py    # Forecast router
    questions.py    # Questions router
    calibration.py  # Calibration router
    admin.py        # Admin-only router (digest, refresh status)
    deps.py         # Shared: admin auth dependency, IP hash helper
```

Add `fastapi`, `uvicorn`, `python-multipart` to `backend/pyproject.toml`.

**Admin auth**

- Admin endpoints require `Authorization: Bearer <ADMIN_API_KEY>` header
- `ADMIN_API_KEY` loaded from `.env` / environment variable
- Return `403` if missing or wrong; no auth library, just a dependency function

**CORS**

- Allow all origins (`*`) for now (public API, Vercel frontend can be any domain)

**Forecast endpoints**

- `POST /forecasts` (admin) — body: `{ question: str, resolution_criteria: str, resolution_source: str, resolution_date: datetime, category: str, submission_gap_days: int = 7 }` — runs agent, saves to DB with initial update row, returns `ForecastResponse`
- `GET /forecasts` — query params: `limit` (default 20), `offset` (default 0) — returns `list[ForecastSummary]`
- `GET /forecasts/{id}` — returns full `ForecastResponse` with decompositions, research, and full update history
- `POST /forecasts/{id}/updates` (admin) — body: `{ probability: float, confidence: str, reasoning: str }` — adds a new update; returns 409 if after `resolution_date`; sets `is_late` flag if within 24h of `resolution_date`
- `PATCH /forecasts/{id}/resolve` (admin) — body: `{ outcome: float | null }` — null means ambiguous; computes `scored_probability` + `brier_score` if not ambiguous; returns updated record
- `POST /forecasts/{id}/refresh` (admin) — manually triggers one refresh cycle for a single forecast outside the daily schedule; same logic as the cron job; returns the new `ForecastUpdateResponse` if an update was written, or `{ updated: false, reason: str }` if the agent found no meaningful change

**Community question endpoints**

- `POST /questions` — body: `{ text: str, resolution_criteria: str, proposed_resolution_date: datetime }` — all three fields required; IP extracted from request and hashed; returns 429 if same IP submitted in last 24h; returns `QuestionResponse`
- `GET /questions` — query params: `status` (optional), `sort` (`score`|`newest`, default `score`), `limit`, `offset` — excludes deleted; caller's vote included in response if `X-Forwarded-For` or `X-Real-IP` header present
- `PUT /questions/{id}` — body: `{ text?: str, resolution_criteria?: str }` — at least one field required; IP must match original submitter; only allowed while status is `pending`; updates `edited_at`
- `DELETE /questions/{id}` — IP must match original submitter; only allowed while status is `pending`; soft-deletes
- `POST /questions/{id}/vote` — body: `{ vote: 1 | -1 }` — IP-based; upserts vote row; to undo a vote, call `DELETE /questions/{id}/vote`
- `DELETE /questions/{id}/vote` — removes caller's vote; returns new net score
- `GET /questions/top-monthly` — returns top 5 by net score among pending/approved questions submitted this calendar month
- `POST /questions/{id}/approve` (admin) — body: `{ resolution_date?: datetime, resolution_criteria?: str }` — admin can override either the proposed resolution date or resolution criteria before approving; sets status to `approved`
- `PUT /questions/{id}` used by admin too — admin can edit `text`, `resolution_criteria`, and `proposed_resolution_date` on any question regardless of status or IP
- `POST /questions/{id}/reject` (admin) — sets status to `rejected`
- `POST /questions/{id}/forecast` (admin) — triggers forecast for approved question; sets status to `forecasted`; links `forecast_id`

**Calibration endpoint**

- `GET /calibration` — returns `CalibrationReport`: Brier score, total resolved, calibration by probability bucket (10 buckets: 0–10%, 10–20%, …, 90–100%)

**Response models (Pydantic, defined in `superforecaster/models.py` and imported by API)**

- `ForecastUpdateResponse`: `id`, `forecast_id`, `probability`, `confidence`, `reasoning`, `is_late`, `created_at`
- `ForecastResponse`: `id`, `question`, `resolution_criteria`, `resolution_source`, `resolution_date`, `submission_deadline`, `category`, `is_ambiguous`, `outcome`, `scored_probability`, `brier_score`, `initial_reasoning`, `decompositions`, `research: ResearchSummary`, `updates: list[ForecastUpdateResponse]`, `last_refreshed_at`, `created_at`, `resolved_at`
- `ForecastSummary`: `id`, `question`, `category`, `resolution_date`, `current_probability`, `confidence`, `is_ambiguous`, `scored_probability`, `last_refreshed_at`, `created_at`, `resolved_at`
- `QuestionResponse`: `id`, `text`, `resolution_criteria`, `proposed_resolution_date`, `net_score`, `user_vote: int | None`, `status`, `edited_at`, `is_deleted`, `created_at`, `forecast_id`
- `VoteResponse`: `question_id`, `net_score`, `user_vote: int | None`
- `CalibrationReport`: `aggregate_brier_score`, `total_resolved`, `total_ambiguous_excluded`, `buckets: list[{ range, predicted_avg, actual_frequency, count }]`

Note: `ip_hash` is **never** included in any response model.

**Run command**

- `uv run uvicorn api.main:app --reload` starts the server

### Success Criteria

- `uv run pytest` passes (include API integration tests using `httpx.AsyncClient`)
- `GET /docs` renders Swagger UI with all endpoints documented
- `POST /questions` → `POST /questions/{id}/vote` → `GET /questions/top-monthly` returns the voted question
- `POST /forecasts` (with valid API key) runs the real agent and returns LLM-generated probability

### Ralph Command

```
/ralph-loop:ralph-loop "Read /Users/narenchaudhry/personal/superforecaster/SPEC.md and implement Spec 2: FastAPI REST API" --max-iterations 30 --completion-promise "uv run pytest passes and all endpoints return correct responses per spec"
```

---

## Spec 3: Next.js Frontend

A Next.js app deployable to Vercel. MUI (Material UI v6) throughout. Clean, minimal, dark-mode aware.

### Prerequisites

- Spec 2 complete

### Requirements

**Project setup**

```
frontend/
  app/
    page.tsx                 # /  — Submit & Vote
    predictions/page.tsx     # /predictions — In-progress forecasts
    resolved/page.tsx        # /resolved — Resolved forecasts
    forecasts/[id]/page.tsx  # /forecasts/[id] — Forecast detail
    admin/page.tsx           # /admin — Admin panel
  components/                # Shared MUI wrappers and layout
  lib/
    api.ts                   # Typed fetch wrappers for every API endpoint
    utils.ts                 # Date formatting, score formatting
  theme.ts                   # MUI theme (light + dark)
```

- Next.js 15+ with App Router, TypeScript
- MUI v6 (`@mui/material`, `@mui/icons-material`)
- No Tailwind
- `NEXT_PUBLIC_API_URL` env var points to FastAPI backend
- `ADMIN_API_KEY` env var — server-side only, used in Server Actions for admin calls

---

**Page: `/` — Submit & Vote**

Layout: full-width list of community question submissions, sorted by net score descending. Question submission form is pinned to the bottom of the viewport (sticky footer).

Question list:
- Each row: question text, `proposed_resolution_date`, net score, upvote/downvote buttons (MUI `IconButton` with thumb icons), status chip
- Clicking a row expands an inline panel (MUI `Collapse`) showing `resolution_criteria` in full — this is the key context voters need to evaluate whether the question is well-defined
- Top 5 rows by net score get a colored MUI `Card` outline (e.g. `primary.main` border) indicating they are in contention for next month's forecasts. Tooltip on hover: "Top 5 — eligible for next batch"
- A user's own vote is reflected immediately (optimistic update); vote is stored by hashed IP server-side
- If a user has already voted, the active vote button is highlighted; clicking again removes the vote; clicking the opposite switches it
- Rows with `status = "forecasted"` show a "Forecast running" chip and link to `/forecasts/[id]`

Question submission form (sticky footer):
- MUI `TextField` for question text (required) — e.g. "Will the US enter a direct military conflict with Iran by end of 2026?"
- MUI `TextField` multiline for resolution criteria (required) — helper text: "Define exactly what counts as YES. Be specific — e.g. 'US troops engage in combat operations on Iranian soil or Iranian-controlled territory, confirmed by two independent news sources.'"
- MUI `DatePicker` for proposed resolution date (required; must be in the future)
- Submit button — calls `POST /questions`; shows 429 error message if rate-limited ("You can submit one question per 24 hours")
- After successful submit: the new question appears at bottom of list with 0 votes
- A user can edit their own submission (pencil icon on their row) or delete it (trash icon) while it is still `pending`; both actions require the same IP
- Edit dialog (MUI `Dialog`): shows all three fields pre-populated; user can update question text and/or resolution criteria; proposed resolution date is not editable after submission (admin controls the final date)

---

**Page: `/predictions` — In-Progress Forecasts**

List of active (unresolved) forecasts, sorted by `resolution_date` ascending (soonest deadline first).

Each row (MUI `Card`):
- Question text
- Category chip
- Current probability (large, bold %)
- Confidence badge (`low`/`medium`/`high`)
- Resolution date
- `last_refreshed_at` ("Last checked: X hours ago")
- Link to `/forecasts/[id]`

No auth required.

---

**Page: `/resolved` — Resolved Forecasts**

List of resolved forecasts, sorted by `resolved_at` descending.

Each row (MUI `Card`):
- Question text + category chip
- Outcome: "YES" or "NO" badge
- Scored probability (time-weighted %)
- Brier score (colored: green if < 0.1, yellow if 0.1–0.2, red if > 0.2)
- Link to `/forecasts/[id]` for full decision log

Aggregate Brier score shown at top of page (fetched from `GET /calibration`).

No auth required.

---

**Page: `/forecasts/[id]` — Forecast Detail**

- Question, resolution criteria, resolution source, category chip, resolution date, submission deadline
- Current probability (large %) + confidence badge
- MUI `Timeline` component showing all updates in chronological order: date, probability, reasoning, late flag if applicable
- Research panel: historical analogs table (description, outcome, relevance), empirical base rate, causal forces list, supporting/contradicting evidence
- Initial decomposition: accordion list of sub-questions with probability and rationale
- If resolved and not ambiguous: outcome badge, scored probability, Brier score with tooltip explaining time-weighted calculation
- If ambiguous: MUI `Alert` — "Excluded from scoring — resolution criteria were ambiguous"

No auth required.

---

**Page: `/admin` — Admin Panel**

- On first visit, shows a MUI `Dialog` prompting for the admin API key; stores it in `localStorage`; all subsequent API calls include it as `Authorization: Bearer <key>`
- MUI `Tabs` with four tabs:

Tab 1 — "Pending Questions":
- Table of `GET /questions?status=pending` sorted by net score
- Each row: question text, resolution criteria (truncated, expandable), proposed resolution date, net score, edit button, approve button, reject button
- Edit opens a MUI `Dialog` with all three fields (`text`, `resolution_criteria`, `proposed_resolution_date`) pre-populated; admin can change any of them
- Approve button opens a confirmation dialog showing the final `resolution_criteria` and lets admin override the `resolution_date` before confirming — this is the last chance to tighten vague criteria before the forecast runs

Tab 2 — "Approved Questions":
- Table of `GET /questions?status=approved`
- Each row: question text, resolution date, "Run Forecast" button → calls `POST /questions/{id}/forecast`; button shows spinner while running

Tab 3 — "Monthly Top":
- Lists `GET /questions/top-monthly` (top 5)
- Each row shows net score; "Approve" button for each
- "Run Digest Now" button at top → calls `POST /admin/digest/run`

Tab 4 — "Forecasts":
- Table of all forecasts with `last_refreshed_at`, current probability, status
- Forecasts with `flagged_for_resolution_review=true` are pinned to the top with an amber MUI `Alert` chip: "Resolution flagged — agent believes this has resolved"
- Each row: "Refresh" button → calls `POST /forecasts/{id}/refresh`; "Resolve" button → opens dialog with outcome input (Yes / No / Ambiguous radio buttons); for flagged forecasts the Resolve button is styled as primary to draw attention
- "Run All Refreshes" button at top → calls `POST /admin/refresh/run`

---

### Success Criteria

- `npx next build` completes without errors
- `/` loads, shows questions sorted by net score, top 5 have colored border
- Submitting a question appears in the list; editing and deleting work while pending
- Voting on a question updates the net score optimistically and persists
- `/predictions` and `/resolved` show real data from the API
- `/forecasts/[id]` shows update timeline and research panel
- Admin can approve, forecast, and resolve questions from `/admin`

### Ralph Command

```
/ralph-loop:ralph-loop "Read /Users/narenchaudhry/personal/superforecaster/SPEC.md and implement Spec 3: Next.js Frontend" --max-iterations 40 --completion-promise "npx next build passes and all five pages render correctly against a running FastAPI backend"
```

---

## Spec 4: Monthly Digest Cron

A scheduled job that surfaces the top-voted community questions at the end of each month for admin review, and auto-promotes them to `approved` status so the admin can trigger forecasts with one click.

### Prerequisites

- Spec 2 complete (Spec 3 recommended but not required)

### Requirements

**Cron job (`superforecaster/cron.py`)**

- Add `APScheduler` to `pyproject.toml` dependencies
- `run_monthly_digest()` function:
  1. Query top 5 `pending` questions by `vote_count` for the current calendar month
  2. Auto-set their status to `approved`
  3. Log a summary: question text, vote count, new status
- Schedule: runs on the last day of each month at 09:00 UTC
- The scheduler starts when the FastAPI app starts (lifespan event in `api/main.py`)

**Admin notification endpoint**

- `GET /admin/digest/preview` (admin) — returns what the monthly digest would promote right now (top 5 pending by votes this month), without mutating state
- `POST /admin/digest/run` (admin) — manually trigger the digest immediately (same logic as the cron job, for testing and emergency use)

**Frontend integration (if Spec 3 is done)**

- Admin Tab 3 "Monthly Top" already shows GET /questions/top-monthly — add a "Run Digest Now" button that calls POST /admin/digest/run

### Success Criteria

- `uv run pytest` passes (include a test that calls `run_monthly_digest()` and verifies top-voted pending questions become `approved`)
- Starting the FastAPI app logs "Monthly digest scheduler started"
- `POST /admin/digest/run` with a valid API key promotes the top 5 pending questions and returns them

### Ralph Command

```
/ralph-loop:ralph-loop "Read /Users/narenchaudhry/personal/superforecaster/SPEC.md and implement Spec 4: Monthly Digest Cron" --max-iterations 20 --completion-promise "uv run pytest passes including digest tests, and POST /admin/digest/run promotes top-voted questions"
```

---

## Spec 5: Daily Forecast Refresh

Once per day, run two sequential sweeps across all active forecasts: first a resolution check, then a probability update. Each sweep uses a dedicated agent. Only non-flagged forecasts proceed to the probability update sweep.

### Prerequisites
- Spec 1 complete
- Spec 2 complete

### Requirements

**Resolution agent (`superforecaster/resolution.py`)**

`ResolutionCheckResult` Pydantic model:
```
appears_resolved:    bool
suggested_outcome:   float | None   # 0.0 or 1.0; None if not resolved
confidence:          str            # "low" | "medium" | "high"
resolution_evidence: str | None     # source URL or summary confirming resolution
reasoning:           str            # why the agent believes this conclusion
```

`resolution_agent` — Pydantic AI agent with `output_type=ResolutionCheckResult`. System prompt instructs it to:
1. Search for direct evidence that the event described in `resolution_criteria` has definitively occurred (outcome = 1.0) or definitively cannot occur (outcome = 0.0)
2. Match evidence against the exact resolution criteria — not the question text, the criteria
3. Set `appears_resolved=True` only when evidence is unambiguous; prefer `confidence="low"` and `appears_resolved=False` when uncertain
4. Never infer resolution from absence of news

`check_resolution(forecast_id: str) -> ResolutionCheckResult`:
1. Load forecast record from DB
2. If already resolved or `is_ambiguous`: return early
3. Run `resolution_agent` with `question`, `resolution_criteria`, `resolution_source`, `resolution_date`
4. If `appears_resolved=True`: set a `flagged_for_resolution_review=True` field on the forecast record and log — **do not auto-resolve**
5. Update `last_refreshed_at`

---

**Refresh agent (`superforecaster/refresh.py`)**

`ForecastRefreshResult` Pydantic model:
```
should_update:    bool
new_probability:  float | None   # only if should_update
new_confidence:   str | None     # "low" | "medium" | "high"
reasoning:        str            # new evidence found, or why no change
evidence_found:   list[str]      # key pieces of new evidence (empty if none)
```

`refresh_agent` — Pydantic AI agent with `output_type=ForecastRefreshResult`. System prompt instructs it to:
1. Search for news on the question from the last `SEARCH_LOOKBACK_HOURS` hours
2. Compare against the current probability and full update history provided as context
3. Set `should_update=True` only for substantive new evidence — not noise, not restating prior reasoning, not absence of news

`refresh_forecast(forecast_id: str) -> ForecastRefreshResult`:
1. Load forecast record + full update history from DB
2. If already resolved, `is_ambiguous`, or `flagged_for_resolution_review=True`: return early, no-op
3. Run `refresh_agent` with question, resolution criteria, current probability, full update history, `SEARCH_LOOKBACK_HOURS`
4. If `result.should_update` AND `abs(result.new_probability - current_probability) >= MIN_PROBABILITY_DELTA`: call `add_forecast_update()`
5. Update `last_refreshed_at`

- `run_daily_refresh() -> RefreshSummary`:
  1. Query all eligible forecasts: `outcome IS NULL AND is_ambiguous = false`
  2. **Sweep 1 — Resolution**: call `check_resolution()` for each, sequentially
  3. **Sweep 2 — Probability**: call `refresh_forecast()` for each forecast not flagged by sweep 1, sequentially
  4. Return `RefreshSummary`: `{ total_checked: int, total_updated: int, total_skipped: int, total_flagged_for_review: int, errors: list[str] }`

**Cron schedule**
- Registered in `api/main.py` lifespan event alongside the monthly digest scheduler
- Default: daily at 06:00 UTC
- Configurable via `REFRESH_CRON_SCHEDULE` env var (cron expression)

**New environment variables**
- `REFRESH_CRON_SCHEDULE` — default `"0 6 * * *"`
- `MIN_PROBABILITY_DELTA` — default `0.03` (3 percentage points)
- `SEARCH_LOOKBACK_HOURS` — default `48`

**Admin endpoint (already in Spec 2)**
- `POST /forecasts/{id}/refresh` — calls `refresh_forecast(id)` and returns the result

**Admin endpoint (new)**
- `POST /admin/refresh/run` (admin) — triggers `run_daily_refresh()` immediately across all eligible forecasts; returns `RefreshSummary`
- `GET /admin/refresh/status` (admin) — returns the result of the last full refresh run (timestamp + `RefreshSummary`)

**Frontend integration (Admin Panel — if Spec 3 is done)**
- Add a "Refresh Now" button on individual forecast rows in the Admin → Forecasts tab; calls `POST /forecasts/{id}/refresh`
- Show `last_refreshed_at` on each forecast row so admin can see when the agent last checked it

### Success Criteria
- `uv run pytest` passes, including:
  - `test_resolution_not_resolved` — agent returns `appears_resolved=False`; `flagged_for_resolution_review` stays false
  - `test_resolution_flags_forecast` — agent returns `appears_resolved=True`; `flagged_for_resolution_review` set to true; forecast NOT auto-resolved
  - `test_resolution_skips_already_resolved` — resolved forecast skipped entirely
  - `test_resolution_skips_ambiguous` — ambiguous forecast skipped entirely
  - `test_refresh_no_update` — `should_update=False`; no new update row; `last_refreshed_at` updated
  - `test_refresh_with_update` — `should_update=True` with ≥ 3% change; new row written
  - `test_refresh_skips_flagged` — forecast flagged by resolution sweep is skipped in probability sweep
  - `test_run_daily_refresh_two_sweeps` — `run_daily_refresh()` runs resolution sweep first, then probability sweep; returns correct counts for both
- Starting the FastAPI app logs "Daily forecast refresh scheduler started (schedule: 0 6 * * *)"
- `POST /admin/refresh/run` returns a valid `RefreshSummary`

### Ralph Command
```
/ralph-loop:ralph-loop "Read /Users/narenchaudhry/personal/superforecaster/SPEC.md and implement Spec 5: Daily Forecast Refresh" --max-iterations 25 --completion-promise "uv run pytest passes including all refresh tests, and POST /admin/refresh/run returns a valid RefreshSummary"
```

