# Backend simplification — audit + plan

## Context

The code works; parts are hard to build on. You asked for a whole-backend audit with three
directives: stop hand-rolling asyncio, drastically simplify `runs.py`, and lean on existing packages
instead of brittle in-memory stores. Must survive: decomposition → parallel base-rates → parallel
inside-views → synthesis → critique, plus parallel runs and stop/resume.

**The rule that drives the display work:** the backend does no data manipulation for the frontend.
Whatever Pydantic produces is streamed out as-is; interpretation happens in the visual layer.

---

## Audit: the LOC census lies

| File | LOC | Executable | Real problem |
|---|---|---|---|
| `db.py` | 1138 | ~1000 | Misfiled logic — ~90 lines of scoring math, 4× duplicated guards, 2 real bugs |
| `checks.py` | 1111 | **~236** | 59% docstrings. Three zip-coupled tables; otherwise sound |
| `models.py` | 956 | ~327 | Junk drawer (deferred — you scoped this out) |
| `runs.py` | 926 | ~600 | Genuinely tangled — 4 unrelated jobs |
| `run_baseline.py` | 617 | **~5** | A JSON fixture wearing a `.py` extension |
| `__main__.py` | 543 | ~500 | `_build_parser()` is 108 lines of argparse |

`checks.py` has 236 executable lines across 36 definitions. Its twelve checks compute genuinely
different things — set difference, log-odds sign analysis, chain-rule arithmetic. **The check bodies
are irreducible**; only their dispatch is duplicated.

### Packages adopted / rejected

| Package | Replaces | Verdict |
|---|---|---|
| `pydantic_graph.beta` `GraphBuilder` | `asyncio.gather` ×2 | ✅ Adopt |
| **DBOS** (`pydantic-ai[dbos]`, `dbos[aiosqlite]`) | all of `checkpoints.py` | ✅ Adopt — in-process, no server |
| `sse-starlette` | hand-rolled heartbeat loop | ✅ Adopt |
| pydantic-ai `run_stream()` / `stream_output()` | the `project_*` flattening | ✅ Adopt |
| `typer` | argparse boilerplate | ✅ Adopt |
| SQLAlchemy / SQLModel / Alembic | raw SQL in `db.py` | ❌ Reject — see below |

### Storage decision: SQLite everywhere, raw SQL retained

Measured against the real file, an ORM eliminates ~200–250 lines and adds ~100–150 of
model/config/Alembic scaffolding — **net 10–15%**. Alembic alone replaces 53 working lines of
`PRAGMA user_version` migration code with a directory, `env.py`, `alembic.ini`, and a versions folder:
a net loss at one migration. ~380 lines of `db.py` are forecasting domain logic no ORM touches.

One caveat worth recording, since it cuts against the decision: **DBOS depends on
`sqlalchemy[asyncio]>=2.0.43` and `psycopg[binary]`**, so SQLAlchemy lands in the tree regardless —
"a large new dependency" is not a valid argument against the ORM. DBOS is also Postgres-first, with
SQLite as a dev-oriented extra.

We take the SQLite path anyway: DBOS on `aiosqlite`, app data on the existing SQLite + raw SQL, two
database files, **no migration of live data**. This suits a single-worker deployment. Revisit if the
app ever needs multiple workers — at which point Postgres and SQLAlchemy Core (not the full ORM)
become the natural move, since the raw SQL is SQLite-dialect (`PRAGMA`, `INSERT OR REPLACE`) and
would need rewriting for Postgres in any case.

---

## Architecture

### The graph, with Reflect promoted

You spotted that `run_reflect` is called at the tail of `run_inside_view`
([inside_view.py:237](backend/superforecaster/agents/inside_view.py:237) and `:268`) — an agentic step
hidden inside a leaf function. It becomes its own node:

```
Decompose ──▶ FindBaseRates ──▶ AdjustInsideView ──▶ Reflect ──▶ Synthesize ──▶ Critique
                  ╱│╲                 ╱│╲                            ▲            │
              map over            map over                           └────────────┘
             sub-claims          sub-claims                        (blocking violations)
                  ╲│╱                 ╲│╱
                 join                join
```

Every box is a `@g.step`; the two `map`/`join` pairs are `GraphBuilder` forks. No `asyncio` anywhere.

### Durability replaces checkpointing

DBOS runs **fully in-process as a library** — no external server, checkpoints to SQLite
(`sqlite:///dbos.sqlite`, via the `aiosqlite` extra), and it already ships inside your installed
pydantic-ai at `durable_exec/dbos/`. Wrap the run in a workflow; every agent call becomes a durable step:

```python
agent = Agent(..., name='outside_view', capabilities=[DBOSDurability()])

@DBOS.workflow()
async def execute_run(run_id: str, input: ForecastInput) -> Forecast:
    return await forecast_graph.run(state=ForecastState(input=input), deps=deps)
```

> "If your program ever fails, when it restarts all your workflows will automatically resume from the
> last completed step."

**This deletes `checkpoints.py` entirely (117 → 0).** No `FileStatePersistence`, no JSON checkpoint
files, no `rewind_for_resume` status-flipping hack, and `resume_run` collapses to a DBOS resume call.
It also unblocks full `GraphBuilder` adoption — durability no longer depends on pydantic-graph
snapshotting, which is what the beta API lacks.

Free bonus: `StepConfig(retries_allowed, max_attempts, backoff_rate)` replaces hand-written retry logic.

### Before → after

```
  api/runs.py       hand-rolled SSE heartbeat        →  sse-starlette EventSourceResponse
  runs.py (926)     pub/sub + registry + lifecycle   →  runs.py (~150): registry + lifecycle
                    + 340 lines of projection           eventstream.py (~70): generic buffer
                                                        (projections mostly deleted)
  checkpoints.py    FileStatePersistence + rewind    →  deleted — DBOS
  graphs/forecast.py  5 BaseNode classes             →  GraphBuilder, 6 steps, 2 map/join forks
  agents/*.py       asyncio.gather ×2                →  gone
  db.py (1138)      persistence + scoring math       →  db.py (~950) + scoring.py (~90)
  checks.py (1111)  3 parallel tables + 144-line     →  one registry; check_evidence deleted
                    check_evidence
  __main__.py (543) argparse                         →  typer
  run_baseline.py   600 lines of dict literals       →  questions.json + ~20-line loader
```

---

## Workstreams

### A. Rebuild the graph on `GraphBuilder` + DBOS

New `graphs/forecast.py` using `GraphBuilder`, `.map()`, and `g.join(reduce_list_append, …)`.
Promote `Reflect` to its own step. Delete `asyncio.gather` from
[inside_view.py:205](backend/superforecaster/agents/inside_view.py:205) and
[outside_view.py:365](backend/superforecaster/agents/outside_view.py:365), along with
`return_exceptions=True`, the `zip(cells, results)` re-pairing, `isinstance(r, BaseException)`
filtering, and the manual first-exception re-raise.

**Must survive:** per-cell `UsageLimitExceeded` degradation (keep it inside the step, return `None`)
and per-cell `sources_seen` isolation — the docs warn *"All parallel tasks share the same graph state."*

**⚠️ Primary risk — prototype this first.** DBOS replays workflows and requires determinism; the docs
note `'parallel'` tool execution "cannot guarantee deterministic ordering". A parallel graph inside a
DBOS workflow is the same class of concern. Mitigation: make each cell a DBOS step keyed on its
sub-claim id, and sort the join output by sub-claim id so state mutation is order-independent.
**Spike this before committing to the rest.**

### B. Split `runs.py` and cut the projection layer

| Lines | Job | Destination |
|---|---|---|
| 89–300 | buffer, seq, pub/sub, thought coalescing | `eventstream.py` (~70, generic) |
| 306–390 | `RunRegistry` | stays (~85) |
| 396–581 | `start` / `execute` / `resume_run` | stays, shrinks (DBOS absorbs resume) |
| 587–926 | `project_*`, `build_waterfall`, `result_payload` | **mostly deleted** |

Per your display rules:

- **`build_waterfall` deleted** (runs.py:855) — no waterfall chart; show the numbers.
- **`check_evidence` deleted** (checks.py:887, 144 lines) — the per-principle explanation payload
  becomes a static FE drawer with hover-highlight. No backend logic.
- **Stage results stream as-is** — `emit(stage, model.model_dump())`. `project_decompose`,
  `project_inside`, `project_synth` collapse to `model_dump()`.
- **Live searches keep streaming** — `query` / `source` / `thought` already come from pydantic-ai's own
  `event_stream_handler` at [observability.py:200](backend/superforecaster/observability.py:200).
  That half is already package-driven; it stays untouched and the FE accumulates it.
- **Grid preserved** — `project_columns` stays (it's the row header emitted at stage start, and it's
  what stops a four-minute research row from rendering blank).

`api/runs.py`: replace the `asyncio.wait_for(queue.get(), …)` generator with
`EventSourceResponse(..., ping=HEARTBEAT_SECONDS)`.

### C. `db.py` — targeted, no ORM

1. Move `compute_time_weighted_probability`, `calibration_report`, decile bucketing (~90 lines) to
   `scoring.py`. It's math, not persistence, and becomes testable without a DB.
2. Extract the 4× question-guard preamble and the 5× `get_question`-and-assert tail (~50 lines).
   **The tail is a real bug** — it opens a second connection after the write txn has committed and closed.
3. Set tz-awareness at the converter level; delete `_ensure_aware` and its 18 call sites (~35 lines).
4. Delete dead `_columns` (:131). Set `PRAGMA user_version` in `init_db` so migrations stop needing
   fresh-DB tolerance — that retires the brittle `"no such column" not in str(e)` string-match.
5. Fix the N+1 in `list_forecasts` (:425 — 51 queries at default limit) and the 3-statement
   `cast_vote` upsert (`INSERT … ON CONFLICT`).
6. Add `PRAGMA journal_mode=WAL` + `busy_timeout` — APScheduler and FastAPI share this DB today.

### D. `checks.py` — one registry

Collapse `FORECAST_CHECK_LABELS` (:866) and the positionally-zipped `violations` list (:1051) into one
registry of `(name, principle, label, run)`. Today a reorder silently mislabels every row. Use lambda
adapters so the narrow signatures `evals/components.py` depends on stay intact. With `check_evidence`
deleted, the third table disappears on its own.

Add `_weighted_mean(pairs)` — three copies of "weighted mean, None when weightless" (:190, :318, :339).
Delete dead aliases `_signed` (:66) and `_spread` (:97).

**Not recommended:** data-fying the check predicates. A config DSL would be harder to read than twelve
functions.

### E. Cheap wins

- `run_baseline.py` → `questions.json` + ~20-line loader. **Deletes ~600 lines.**
- `__main__.py` → typer; `_build_parser()` (:425–533) evaporates.
- Delete `ForecastResearchNotes` (models.py:200) — zero references repo-wide.

**Deferred (you scoped it out):** splitting `models.py` by consumer.

---

## Order of work

1. **Spike A's DBOS-under-parallel-graph determinism** — everything else depends on it.
2. A — graph rebuild + Reflect node.
3. B — `runs.py` split, projection cut, sse-starlette.
4. C, D, E — independent, any order.

---

## Verification

1. `cd backend && uv run pytest`. Expect real churn in `test_fanout.py` (495 lines — it asserts the
   gather barrier directly), `test_checkpoints.py` (241 — the module is being deleted),
   `test_runs.py` (580), `test_graph_stream.py` (154).
2. **Fan-out intact:** multi-column question — each cell's `sources_seen` stays private until the join;
   one column hitting `UsageLimitExceeded` degrades that column while the row completes.
3. **Reflect is its own node:** it appears in the rendered graph and fires its own stage events.
4. **Durability:** start a run, `kill -9` the process mid-`Synthesize`, restart, confirm DBOS resumes
   from the last completed step and does **not** re-pay for decomposition or research.
5. **Stream:** `POST /runs` → `GET /runs/{id}/stream` → strictly increasing `seq`, heartbeats when idle,
   `?from_seq=` replay after reconnect, live search events arriving during research.
6. Frontend smoke test in a browser — no build step and no types, so nothing catches a rename but the eye.
7. `uv run superforecaster diagram` renders the 6-node graph.

---

## Open items for a later pass

- **Frontend audit** — you asked for one; it's not in this plan's scope. The FE is 1,651 lines of
  untyped vanilla JS with no build step, and it owns the principles drawer, hover-highlight, and search
  accumulation this plan hands it.
- `models.py` consumer split.