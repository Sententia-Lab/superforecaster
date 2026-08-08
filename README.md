# Superforecaster

Make testable forecasts about the future.

Built on PydanticAI and based on Philip Tetlock's Superforecasting methodology.

You write a question in plain prose. The AI drafts it into a resolvable question, then a
gated pipeline breaks it into sub-questions, counts a base rate for each reference
population, adjusts each from its own evidence, and commits to a probability — one stage
per click, showing the whole walk from anchor to answer, with every step persisted.

The 16 principles are enforced as **checks over typed output**, not as prompt instructions.
A forecast that skips the outside view, or lands on a number its own adjustments do not
imply, gets sent back. See [`spec/superforecasting_methodology.md`](spec/superforecasting_methodology.md).

---

## Run it

You need [uv](https://docs.astral.sh/uv/), Node, and two keys:

| | | |
|---|---|---|
| **LLM** | [console.anthropic.com](https://console.anthropic.com/) | the model |
| **Tavily** | [tavily.com](https://tavily.com) | web search — free tier is enough |
| **Wikipedia** | [wikipedia.com](https://pypi.org/project/Wikipedia-API/) | general knowledge |

```bash
make install
make dev
```

Open **http://localhost:5173**, click **Keys** in the header, and paste them in. That is
the whole setup — no file to write, no admin token to invent, no database to create.

Keys set in that panel live in the server process and are dropped when it restarts. To
have them survive, set `ANTHROPIC_API_KEY` and `TAVILY_API_KEY` in the environment you
start from — see [Configuration](#configuration) for every variable that matters.

The startup banner tells you exactly what you got:

```
Superforecaster
  model         anthropic:claude-sonnet-4-6
  web search    Tavily
  admin auth    local mode — unauthenticated requests from localhost only
  database      ./superforecaster.db
```

and `make config` shows every setting, where its value came from, and which one is
shadowing which — with the secrets redacted.

<details>
<summary>Running without a Tavily key</summary>

It still works — the agents fall back to Wikipedia alone. The run completes and the checks
still hold, but the reference classes come out noticeably thinner, which is the difference
between a base rate and a guess with a citation. The header shows a **no web search** chip
and the startup banner says the same, so you never get that silently.
</details>

<details>
<summary>Using the Logfire Gateway instead of Anthropic directly</summary>

Set `PYDANTIC_AI_GATEWAY_API_KEY` in the environment, from
[logfire.pydantic.dev](https://logfire.pydantic.dev) → your org → **Gateway**. It takes
precedence over `ANTHROPIC_API_KEY`. Legacy `paig_...` keys no longer work, and this is
for LLM calls only — it does not send traces.

The Keys panel follows whichever variable credentials the model, so it names the gateway
key once one is set and `ANTHROPIC_API_KEY` otherwise. It cannot switch you *onto* the
gateway — that first key has to come from the environment. `make config` says which one
is in play.
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

Every stage collapses, and a finished run leads with its answer. Each measured cell
restates the lens it was measured through, so what came from the lens stays separate from
what the cell found. The synthesis table lays out every lens and every modifier in one
place.

The connection is the agent's lifetime: close the laptop and the in-flight step stops,
lands as `cancelled`, and is one click to re-run. A step that fails is retryable from the
database — there is no separate checkpoint system. `make forecast` runs the same stages
back-to-back with no gates.

---

## Commands

`make` on its own lists all of them.

| | |
|---|---|
| `make install` | backend and frontend dependencies |
| `make dev` | backend :8099 + frontend :5173, both hot-reloading, one Ctrl+C stops both |
| `make backend` / `make frontend` | either one alone |
| `make serve` | builds the frontend, then serves the whole app as one process on :8000 |
| `make build` | the frontend into `frontend/dist` |
| `make test` | the backend suite — no network, no API keys |
| `make clean` | build output, `node_modules`, and the venv |

The CLI runs the same stages without the browser:

| | |
|---|---|
| `make forecast` | one forecast, interactive prompts, saved to SQLite |
| `make smoke` | a bundled question, shallow research, nothing saved — the cheap end-to-end check |
| `make config` | every setting and **where its value came from** — secrets redacted |
| `make diagram` | the pipeline shape, as mermaid |
| `make refresh ID=<uuid>` | re-check a saved forecast against new evidence |
| `make resolve ID=<uuid>` | has it resolved yet? |
| `make cli ARGS="--help"` | everything else |

**Docker**, if you would rather not install `uv` and Node at all. Set your keys **and**
an `ADMIN_API_KEY` in the environment first — a container is not localhost, so the Keys
panel cannot authenticate you into one that has no admin key:

| | |
|---|---|
| `make docker` | the whole app in one container on :8000 |
| `make docker-dev` | containerized hot-reload: frontend :5173, api :8000 |
| `make docker-down` | stop the stack |

`make docker` needs no separate build step. The image builds the frontend in a Node stage
and copies `dist/` in, then serves it from FastAPI, SQLite in the `sqlite_data` volume.
Still ADR 47's "one process serving everything" — the frontend build just moved from your
machine into the image, so a fresh clone with nothing installed but Docker works.
`make docker-dev` adds a Vite container proxying to the API over the compose network; it
is opt-in, so the production path stays the single-process deploy.

---

## Configuration

Beyond the two keys, nothing here is required. Every setting is an environment variable,
and `make config` prints all of them with their origins.

| Variable | Why you would set it |
|---|---|
| `ANTHROPIC_API_KEY` | The model. Or `PYDANTIC_AI_GATEWAY_API_KEY` to route through Logfire |
| `TAVILY_API_KEY` | Web search for every research agent |
| `WIKIPEDIA_API_KEY` | Optional. Raises the Wikimedia rate limit; nothing needs it |
| `ADMIN_API_KEY` | **Required to serve this anywhere but your own machine** — see below |
| `AGENT_MODEL` | Override the model for every agent, e.g. `anthropic:claude-sonnet-4-6` |
| `LOGFIRE_TOKEN` | A `pylf_v1_...` *write* token for cloud traces. Different from the gateway key |
| `CELL_SOFT_CALLS_PER_ITERATION` | The cline — searches per unit of depth before an agent is pushed to commit (default 1) |
| `CELL_HARD_HEADROOM` | Calls between the cline and the hard cap (default 3) |
| `DATABASE_PATH` | Default `./superforecaster.db`; Docker uses `/app/data/` |

Every check threshold is an environment variable too — see
[`spec/CURRENT_STATE.md`](spec/CURRENT_STATE.md).

The LLM, Tavily, and Wikipedia keys can also be set from the **Keys** panel in the header.
Those apply on the next request and are dropped when the process restarts; no route ever
returns a key's value, only where it came from.

### Admin auth

Starting a forecast is an admin action. With `ADMIN_API_KEY` unset, the API accepts
unauthenticated admin requests **from 127.0.0.1 only**, and refuses them everywhere else
with a message saying why. That is what makes the two-key setup work: on a laptop, where
the only thing that can reach the port is the person who started the process, a token
protects nothing and costs the entire first-run experience.

A request carrying any proxy header (`X-Forwarded-For` and friends) is never treated as
local — a reverse proxy in front of this is the shape of a real deployment, and anything
upstream can rewrite the origin. **Set `ADMIN_API_KEY` before exposing the port.** Once
set, paste the same value into the **Keys** panel.

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

## Where things are

- **What exists and what it does** → [`spec/CURRENT_STATE.md`](spec/CURRENT_STATE.md)
- **Why it is shaped this way** → [`spec/ADR.md`](spec/ADR.md)
- **What the agents are supposed to do** → [`spec/superforecasting_methodology.md`](spec/superforecasting_methodology.md)

---

## Observability

With a `LOGFIRE_TOKEN` set, runs send structured traces to
[logfire.pydantic.dev](https://logfire.pydantic.dev) — filter by service `superforecaster`
and expand the per-column research spans. Without one, `make cli ARGS="forecast --verbose"`
prints the same tool calls and results to the terminal.

---

## References

- Tetlock, P. E., & Gardner, D. (2015). *Superforecasting: The Art and Science of Prediction*
- [Pydantic AI](https://ai.pydantic.dev/)

## License

MIT
