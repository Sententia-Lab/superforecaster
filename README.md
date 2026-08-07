# Superforecaster

Forecasting agents that implement Tetlock's superforecasting methodology, in Pydantic AI.

You write a question in plain prose. The AI drafts it into a resolvable question, then a
gated pipeline breaks it into sub-questions, counts a base rate for each reference
population, adjusts each from its own evidence, and commits to a probability — one stage
per click, showing the whole walk from anchor to answer, with every step persisted.

The 16 principles are enforced as **checks over typed output**, not as prompt instructions.
A forecast that skips the outside view, or lands on a number its own adjustments do not
imply, gets sent back. See [`spec/superforecasting_methodology.md`](spec/superforecasting_methodology.md).

---

## Run it

Two keys and [uv](https://docs.astral.sh/uv/):

| | | |
|---|---|---|
| **LLM** | [console.anthropic.com](https://console.anthropic.com/) | the model |
| **Tavily** | [tavily.com](https://tavily.com) | web search — free tier is enough |

```bash
git clone <this repo> && cd superforecaster/backend
uv sync

export ANTHROPIC_API_KEY=sk-ant-...
export TAVILY_API_KEY=tvly-...

uv run python -m superforecaster serve
```

Build the frontend once first (`cd ../frontend && npm install && npm run build`), then
open **http://localhost:8000**. That is the whole setup — no `.env` to write, no admin
token to invent, no database to create. (For frontend development, `npm run dev` starts a
Vite server that proxies to the API on :8099.)

Or, from the repo root: `make install && make dev` runs both hot-reloading, or
`docker compose up --build` if you'd rather not install `uv`/`npm` at all — see
[Commands](#commands) for both.

The startup banner tells you exactly what you got:

```
Superforecaster
  model         anthropic:claude-sonnet-4-6
  web search    Tavily
  admin auth    local mode — unauthenticated requests from localhost only
  database      ./superforecaster.db
```

To make the keys persist, `cp .env.example .env` and fill it in. Real environment
variables always win over that file — `superforecaster config` shows which is which:

```
  setting                          origin       value
  ANTHROPIC_API_KEY                .env         set (108 chars)
  TAVILY_API_KEY                   environment  set (37 chars)   <- an export is shadowing .env
  AGENT_MODEL                      unset        —

  resolved model                   anthropic:claude-sonnet-4-6
```

<details>
<summary>Running without a Tavily key</summary>

It still works — the agents fall back to Wikipedia alone. The run completes and the checks
still hold, but the reference classes come out noticeably thinner, which is the difference
between a base rate and a guess with a citation. The header shows a **no web search** chip
and the startup banner says the same, so you never get that silently.
</details>

<details>
<summary>Using the Logfire Gateway instead of Anthropic directly</summary>

```bash
export PYDANTIC_AI_GATEWAY_API_KEY=pylf_v2_...
```

From [logfire.pydantic.dev](https://logfire.pydantic.dev) → your org → **Gateway**. Legacy
`paig_...` keys no longer work. This is for LLM calls only — it does not send traces.
</details>

---

## What a run looks like

A run is a **persisted machine of gated stages** — nothing executes unless you click next,
and every stage output lands in SQLite the moment it exists.

```
 1  Decompose      one agent, live tail          → 3–5 sub-questions   [review, next]
 2  Find lenses    one agent per sub-question    → 1–3 populations each [all must finish]
 3  Base rates     one agent per (sub-Q, lens)   → a COUNTED rate Σhits/Σn  [per-cell next]
 4  Inside view    one agent per (sub-Q, lens)   → signed modifiers on THAT lens's rate
 5  Synthesis      arithmetic (not agentic), then reflect + synthesize + pure critique
```

Each cell streams its searches inside its own card while it works. The anchor is the
**chain** the decomposition describes — the product of the per-column rates for a
conjunction, not an average of lenses pointed at different questions — and the final
probability may deviate from the implied number by at most ±5 points
(`CHECK_DERIVATION_SLACK`).

The connection is the agent's lifetime: close the laptop and the in-flight step stops,
lands as `cancelled`, and is one click to re-run. A step that fails is retryable from the
database — there is no separate checkpoint system. `superforecaster forecast` (CLI) runs
the same stages back-to-back with no gates.

---

## Commands

All from `backend/`.

| | |
|---|---|
| `uv run python -m superforecaster serve` | API + web UI on :8000 |
| `uv run python -m superforecaster serve --port 9000 --reload` | pick a port, restart on edits |
| `uv run python -m superforecaster forecast` | one forecast, interactive prompts, saved to SQLite |
| `uv run python -m superforecaster forecast --fixture --no-save -v` | smoke test on a bundled question, prints JSON |
| `uv run python -m superforecaster forecast --fixture --max-iterations 3` | shallower research — cheaper and faster |
| `uv run python -m superforecaster refresh --id <uuid>` | re-check an existing forecast against new evidence |
| `uv run python -m superforecaster resolve --id <uuid>` | has this resolved yet? |
| `uv run python -m superforecaster config` | every setting and **where its value came from** — secrets redacted |
| `uv run python -m superforecaster diagram` | the pipeline shape, as mermaid |
| `uv run pytest` | the whole suite — no network, no API keys |
| `uv run python -m superforecaster --help` | everything else |

**Docker**, from the repo root. Put both keys **and** an `ADMIN_API_KEY` in `backend/.env`
first — a container is not localhost:

```bash
docker compose up --build
```

That's the whole thing — no `npm run build` first. The image builds the frontend in a
Node stage and copies `dist/` in, then serves it from FastAPI on :8000, SQLite in the
`sqlite_data` volume. Still ADR 47's "one process serving everything" — the frontend build
just moved from your machine into the image, so a fresh clone with nothing installed but
Docker works.

<details>
<summary>Dockerized frontend dev loop (hot reload, no local Node needed)</summary>

```bash
docker compose --profile dev up --build
```

This also starts a `frontend` service (`frontend/Dockerfile.dev`) running `npm run dev` on
**:5173**, proxying API routes to the `api` service over the compose network. It's opt-in
via the `dev` profile — plain `docker compose up` never starts it, so production stays the
single-process deploy above.
</details>

**Makefile**, wraps the commands above:

| | |
|---|---|
| `make install` | `uv sync` + `npm install` |
| `make dev` | backend on :8099 and `npm run dev` on :5173, both hot-reloading, one Ctrl+C stops both |
| `make backend` / `make frontend` | either one alone |
| `make build` | `npm run build` |
| `make test` | `uv run pytest` |
| `make docker` | `docker compose up --build` |
| `make docker-dev` | `docker compose --profile dev up --build` |
| `make docker-down` | stop the compose stack |

Every target that touches the backend depends on `backend/.env` and creates it from
`backend/.env.example` if missing — you still have to fill in the keys yourself.

---

## Configuration

Beyond the two keys, nothing here is required. Real environment variables beat
`backend/.env`.

| Variable | Why you would set it |
|---|---|
| `ANTHROPIC_API_KEY` | The model. Or `PYDANTIC_AI_GATEWAY_API_KEY` to route through Logfire |
| `TAVILY_API_KEY` | Web search for every research agent |
| `ADMIN_API_KEY` | **Required to serve this anywhere but your own machine** — see below |
| `AGENT_MODEL` | Override the model for every agent, e.g. `anthropic:claude-sonnet-4-6` |
| `LOGFIRE_TOKEN` | A `pylf_v1_...` *write* token for cloud traces. Different from the gateway key |
| `CELL_SOFT_CALLS_PER_ITERATION` | The cline — searches per unit of depth before an agent is pushed to commit (default 1) |
| `CELL_HARD_HEADROOM` | Calls between the cline and the hard cap (default 3) |
| `DATABASE_PATH` | Default `./superforecaster.db`; Docker uses `/app/data/` |

Full list in [`backend/.env.example`](backend/.env.example); every check threshold is an
env var too — see `spec/CURRENT_STATE.md`.

### Admin auth

Starting a forecast is an admin action. With `ADMIN_API_KEY` unset, the API accepts
unauthenticated admin requests **from 127.0.0.1 only**, and refuses them everywhere else
with a message saying why. That is what makes the two-key setup work: on a laptop, where
the only thing that can reach the port is the person who started the process, a token
protects nothing and costs the entire first-run experience.

A request carrying any proxy header (`X-Forwarded-For` and friends) is never treated as
local — a reverse proxy in front of this is the shape of a real deployment, and anything
upstream can rewrite the origin. **Set `ADMIN_API_KEY` before exposing the port.** Once
set, the UI shows an **Admin** button; paste the same value there.

### Search budget

Each cell — one column at one research stage — gets its own two-tier budget:

```
soft_depth = max_iterations × CELL_SOFT_CALLS_PER_ITERATION    # the cline
hard_depth = soft_depth + CELL_HARD_HEADROOM                   # the wall
```

Past the cline the agent is told, in the tool result itself, to stop searching and write
its answer. The wall is `UsageLimits.tool_calls_limit`. A cell that hits it degrades to no
result and the run continues — one greedy column no longer costs the others their work.

---

## Layout

```
backend/
  superforecaster/
    agents/          decompose, lenses, outside_view, inside_view, reflect, synthesize, …
    stages.py        the per-stage functions + run_all (CLI/eval auto-advance)
    machine.py       the gated state machine — every legal transition
    graphs/          the update graph (resolution checks + Bayesian updates)
    checks.py        the 16 principles as pure functions over typed output
    db.py            SQLite: forecasts, gated_runs, run_steps — with schema migrations
  api/               FastAPI routes, including /runs and its per-step SSE stream
  config.py          settings, budgets, check thresholds — every number an env var
frontend/            React + Vite: src/ components, derive.js mirrors checks.py
spec/                CURRENT_STATE.md (what exists), ADR.md (why)
Makefile             install / dev / build / test / docker / docker-dev
docker-compose.yml   api (prod, builds frontend in-image) + frontend (dev profile only)
```

- **What exists and what it does** → [`spec/CURRENT_STATE.md`](spec/CURRENT_STATE.md)
- **Why it is shaped this way** → [`spec/ADR.md`](spec/ADR.md)
- **What the agents are supposed to do** → [`spec/superforecasting_methodology.md`](spec/superforecasting_methodology.md)

---

## Observability

With a `LOGFIRE_TOKEN` set, runs send structured traces to
[logfire.pydantic.dev](https://logfire.pydantic.dev) — filter by service `superforecaster`
and expand the per-column research spans. Without one, `--verbose` prints the same tool
calls and results to the terminal.

---

## References

- Tetlock, P. E., & Gardner, D. (2015). *Superforecasting: The Art and Science of Prediction*
- [Pydantic AI](https://ai.pydantic.dev/)

## License

MIT
