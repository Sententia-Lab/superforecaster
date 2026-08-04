# Superforecasting Agent

A forecasting platform that implements Tetlock's superforecasting methodology using Pydantic AI. The backend runs nine agents behind two graphs, a FastAPI API, and a CLI. The frontend is a zero-build static app served by the API — you submit a question in plain prose, watch the forecast graph reason through it live over SSE, and get a probability with the anchor-to-stated walk that produced it.

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | latest | Python deps and backend commands |
| Python | ≥ 3.12 | Backend + agents |
| Docker + Compose | optional | Full-stack deployment |

---

## Setup

### 1. Install backend dependencies

```bash
cd backend
uv sync
```

### 2. Configure environment

Copy the example env files and fill in keys:

```bash
cp backend/.env.example backend/.env
```

**Backend** (`backend/.env`) — minimum to run agents:

| Variable | Required | Purpose |
| --- | --- | --- |
| `PYDANTIC_AI_GATEWAY_API_KEY` | One of these | Logfire Gateway key (`pylf_v2_...`) from [logfire.pydantic.dev](https://logfire.pydantic.dev) → Org → **Gateway** |
| `ANTHROPIC_API_KEY` | One of these | Direct Anthropic API (bypasses gateway) |
| `TAVILY_API_KEY` | Recommended | Web search for research phase |
| `RESEARCH_TOOL_CALLS_PER_ITERATION` | No | Tool-call budget per unit of search depth (default 3) |
| `RUN_CHECKPOINT_DIR` | No | Where graph snapshots live, so a failed run can resume (default `./run_checkpoints`) |
| `ADMIN_API_KEY` | For admin API/UI | Bearer token for `/admin/*` routes |
| `LOGFIRE_TOKEN` | No | Logfire **write token** (`pylf_v1_...`) for cloud traces — separate from gateway key |

Legacy `paig_...` gateway keys no longer work. See the [Logfire Gateway migration guide](https://pydantic.dev/docs/logfire/gateway-migration/).

The frontend needs no configuration — it is static files served same-origin by the API.

All backend settings are loaded from `backend/.env` via `backend/config.py`.

---

## How to Run

All backend commands assume you are in the `backend/` directory:

```bash
cd backend
```

### CLI — run agents directly

The CLI runs agents without starting the web server. Output is formatted JSON on stdout.

#### Forecast (new question)

**Interactive** — prompts for question, criteria, source, date, category; saves to SQLite:

```bash
uv run python -m superforecaster forecast
```

**Fixture smoke test** — uses bundled `superforecaster/fixtures/forecast_question.json`:

```bash
uv run python -m superforecaster forecast --fixture
```

**No database write** — print forecast JSON only:

```bash
uv run python -m superforecaster forecast --fixture --no-save
```

**Custom fixture file:**

```bash
uv run python -m superforecaster forecast --fixture path/to/question.json
```

**Watch progress in the terminal** (tool calls, usage limits):

```bash
uv run python -m superforecaster forecast --fixture --no-save --verbose
```

**Limit research budget** (default `5`; lower = cheaper/faster):

```bash
uv run python -m superforecaster forecast --fixture --no-save --max-iterations 3
```

When saving to the database, the forecast UUID is printed to stderr:

```json
{"forecast_id": "..."}
```

#### Refresh (update probability on existing forecast)

**In-memory fixture** (no DB write):

```bash
uv run python -m superforecaster refresh --fixture
```

**From database** by UUID:

```bash
uv run python -m superforecaster refresh --id <forecast-uuid>
```

Add `--verbose` to either command for terminal progress.

#### Resolve (check if a forecast should be flagged for resolution)

**In-memory fixture:**

```bash
uv run python -m superforecaster resolve --fixture
```

**From database:**

```bash
uv run python -m superforecaster resolve --id <forecast-uuid>
```

#### CLI help

```bash
uv run python -m superforecaster --help
uv run python -m superforecaster forecast --help
```

### API — backend server (local)

Starts FastAPI on port 8000. Also starts scheduled jobs (daily refresh, monthly digest) via APScheduler.

```bash
cd backend
uv run uvicorn api.main:app --reload
```

| URL | Purpose |
| --- | --- |
| http://localhost:8000/docs | Swagger UI |
| http://localhost:8000/healthz | Health check |

**Create a forecast via API** (requires `ADMIN_API_KEY` in `backend/.env`):

```bash
curl -X POST http://localhost:8000/forecasts \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Will X happen?",
    "resolution_criteria": "Resolves YES if ...",
    "resolution_date": "2026-12-31T00:00:00Z",
    "resolution_source": "Official source",
    "category": "politics"
  }'
```

Public routes (`GET /forecasts`, `GET /questions`, etc.) need no auth. Admin routes use `Authorization: Bearer <ADMIN_API_KEY>`. See `spec/CURRENT_STATE.md` for the full route list.

### Frontend — web UI (local)

No install and no build. Start the API and open it:

```bash
cd backend && uv run uvicorn api.main:app --reload
```

Open http://localhost:8000

| Page | Purpose |
| --- | --- |
| `/` | Draft a question, watch a run stream, read saved results |
| `/admin.html` | Approve questions, run forecasts, refresh, resolve |

Starting a run is admin-gated. Click **Admin** in the header and paste the same value as
`ADMIN_API_KEY`; it is stored in browser `localStorage`. Watching a run needs no token.

Your backlog, finished results, and the reasoning trail of each run are kept in
`localStorage` (the last 12 trails). A run that fails part-way keeps a server-side
checkpoint — **Resume** re-runs only the step that died.

### Full stack — Docker

From the repo root:

```bash
cp backend/.env.example backend/.env   # fill in keys
docker compose up --build
```

One service. The frontend is bind-mounted into the API container and served at `/`.

| Service | URL |
| --- | --- |
| App + API | http://localhost:8000 |
| Admin | http://localhost:8000/admin.html |
| Swagger | http://localhost:8000/docs |

SQLite data persists in the `sqlite_data` Docker volume (`DATABASE_PATH=/app/data/superforecaster.db` inside the API container).

**Detached mode:**

```bash
docker compose up --build -d
```

**Stop:**

```bash
docker compose down
```

### Tests

**Backend** (313 tests, no network and no API keys needed):

```bash
cd backend
uv run pytest
```

---

## Forecast pipeline and usage limits

Forecasting is a five-node graph — decompose → find base rates → adjust (inside view) →
synthesize → critique — with one retry edge from critique back to synthesis. See
`spec/CURRENT_STATE.md` for the wiring.

The two researching steps (base rates, inside view) are tool-using and their budget scales
with search depth (`--max-iterations`, default 5):

```
request_limit    = depth × RESEARCH_REQUESTS_PER_ITERATION + 1     # default 3 → 16
tool_calls_limit = depth × RESEARCH_TOOL_CALLS_PER_ITERATION       # default 3 → 15
```

Synthesis is tool-free with its own small budget (4 requests). Refresh and resolution use
`AGENT_REQUEST_LIMIT` / `AGENT_TOOL_CALLS_LIMIT` (40 requests, 20 tool calls).

**Exhausting a budget raises `UsageLimitExceeded` and kills that node** — it does not degrade
into a partial answer. That is why runs are checkpointed: the completed steps are kept, and
resuming re-runs only the one that failed. Raise the budget on the way back in, either with a
higher search depth on resume or by raising `RESEARCH_TOOL_CALLS_PER_ITERATION`.

Optional env overrides in `backend/.env`:

```bash
AGENT_MODEL=gateway/anthropic:claude-sonnet-4-6
AGENT_REQUEST_LIMIT=40
AGENT_TOOL_CALLS_LIMIT=20
RESEARCH_TOOL_CALLS_PER_ITERATION=3
RESEARCH_REQUESTS_PER_ITERATION=3
```

---

## Observability (Logfire)

With a valid `LOGFIRE_TOKEN` (`pylf_v1_...`), agent runs send structured traces to Logfire automatically.

1. Open [logfire.pydantic.dev](https://logfire.pydantic.dev) → your project → **Live** or **Explore**
2. Filter by tag `agent-progress` or service `superforecaster`
3. Expand spans for `forecast research` / `forecast synthesis` to see reasoning, tool calls, and results

Terminal progress is available with `--verbose` on CLI commands.

The gateway key (`PYDANTIC_AI_GATEWAY_API_KEY`) is for LLM calls only — it does not send traces to Logfire.

---

## Programmatic usage

```python
import asyncio
from datetime import datetime, timezone

from superforecaster.agent import run_forecast
from superforecaster.models import ForecastInput

async def main() -> None:
    result = await run_forecast(
        ForecastInput(
            question="Will Bitcoin exceed $100k by end of 2026?",
            resolution_criteria="BTC/USD spot price on a major exchange exceeds $100,000 before 2027-01-01 UTC.",
            resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
            category="finance",
            max_iterations=5,
        ),
        verbose=False,
    )
    print(f"Forecast: {result.probability:.0%}")
    print(f"Confidence: {result.confidence}")
    print(f"Reasoning: {result.reasoning}")

asyncio.run(main())
```

Run from `backend/` so `config.py` loads `.env`.

---

## Repository layout

```
backend/
  superforecaster/   # Agents, graphs, models, tools, checks, DB, cron
    runs.py          #   live-run registry + typed state -> SSE events
  api/               # FastAPI routes, including /runs and its SSE stream
  config.py          # Settings from .env
frontend/            # static app — index.html, app.js, api.js, admin.html
spec/                # Spec-driven docs (CURRENT_STATE.md, ADR.md)
docker-compose.yml
```

For architecture details, data models, and the full API surface, see `spec/CURRENT_STATE.md`. For why the system is shaped this way, see `spec/ADR.md`.

---

## Methodology

The agents follow Tetlock's superforecasting principles:

1. **Triage** — focus on forecastable questions
2. **Break down** — Fermi-ize into sub-questions
3. **Outside view first** — historical analogs and base rates
4. **Inside view second** — case-specific evidence
5. **Granular probabilities** — e.g. 65%, not "likely"
6. **Separate confidence** — certainty distinct from probability
7. **Iterate** — refresh on new evidence; track calibration

Full agent behavior spec: `spec/superforecasting_methodology.md`.

---

## References

- Tetlock, P. E., & Gardner, D. (2015). *Superforecasting*
- [Pydantic AI](https://ai.pydantic.dev/)
- [Logfire Gateway migration](https://pydantic.dev/docs/logfire/gateway-migration/)

## License

MIT
