# Current State — Data Lineage

**This document has one job: trace every byte from a user action, through each function that
touches it, into SQLite, and back to the screen.** If you want to know where a number came
from or where a click ends up, it is in here.

Deliberately not here:
- **Repository layout** — read the tree.
- **How to run** — `README.md`.
- **Data models** — `backend/superforecaster/models.py`.
- **Environment variables** — `backend/.env.example` and `backend/config.py`.
- **Dependencies** — `backend/pyproject.toml`.
- **Why it is shaped this way** — `spec/ADR.md`.
- **The 16 principles** (`P<n>` below) — `spec/superforecasting_methodology.md`.

Last regenerated: 2026-08-07.

---

## Storage map

Everything persists in four SQLite tables (`backend/superforecaster/db.py`, `SCHEMA_VERSION = 3`).

| Table | One row means | Written by |
|---|---|---|
| `gated_runs` | one forecast question + its lifecycle | `create/update/start/complete/delete_gated_run` |
| `run_steps` | one stage cell (`UNIQUE(run_id, stage, sub_claim_id, lens_name)`) | `insert/claim/finish/fail/delete_steps`, `edit_step_payload` |
| `forecasts` | a *published* forecast, written only at synthesis | `save_forecast`, `resolve_forecast`, `mark_refreshed` |
| `forecast_updates` | probability history for one forecast | `save_forecast`, `add_forecast_update` |

Run status: `backlog → active → complete`. Step status: `pending → running → complete|error`.
`gated_runs.error` mirrors the latest failing step — a red chip, not a status.
`run_steps.edited_at` is set when a person replaced a payload by hand (§3g); it never moves
`status` or `attempts`.

Two lifecycles meet at exactly one point: a gated run produces a `forecasts` row at synthesis,
and from then on the forecast lives independently (delete the run, the forecast survives).

---

## Endpoint index

All 21 routes, each traced in the section named.

| Method + path | Auth | Traced in |
|---|---|---|
| `GET /config` | — | §0 App boot |
| `GET /runs` | — | §0 App boot |
| `POST /questions/draft` | — | §1 Drafting |
| `POST /questions/critique` | — | §1 Drafting |
| `POST /runs` | — | §2 Create & start |
| `PATCH /runs/{id}` | — | §2 Create & start |
| `POST /runs/{id}/start` | admin | §2 Create & start |
| `POST /runs/{id}/steps/{step_id}/stream` | admin | §3 Running one step |
| `PUT /runs/{id}/steps/{step_id}/payload` | admin | §3g Editing a payload |
| `GET /runs/{id}` | — | §4 Reload |
| `DELETE /runs/{id}` | admin | §5 Delete |
| `POST /forecasts` | admin | §6 Ungated pipeline |
| `GET /forecasts` | — | §7 Forecast reads |
| `GET /forecasts/{id}` | — | §7 Forecast reads |
| `POST /forecasts/{id}/updates` | admin | §8 Update, resolve, refresh |
| `PATCH /forecasts/{id}/resolve` | admin | §8 Update, resolve, refresh |
| `POST /forecasts/{id}/refresh` | admin | §8 Update, resolve, refresh |
| `GET /calibration` | — | §9 Calibration |
| `POST /admin/refresh/run` | admin | §10 Cron sweep |
| `GET /healthz` | — | §11 Health & static |
| `GET /` (+ all static) | — | §11 Health & static |

Sections §6–§10 are reachable but the React app never calls them — they are the CLI's API twin
and the cron surface. That is noted per section rather than hidden in a footnote.

---

## 0. App boot — `GET /config`, `GET /runs`

```
mount App.jsx
  ├─ api.config()      GET /config       → { auth_required, search_enabled, model }
  └─ useRuns.refresh() GET /runs         → [ GatedRunSummary + stage_counts ]
```

| Layer | What happens |
|---|---|
| FE | `App.jsx:26` sets `config`; `useRuns.js:9` fills the sidebar |
| API | `main.py:97 client_config` → `is_local_mode(request)` (`deps.py:15`) |
| API | `runs.py:60 list_runs` → `db.list_gated_runs` |
| DB | two SELECTs: all runs newest-first, plus a `GROUP BY run_id, stage, status` count roll-up |
| Back | `auth_required` is the load-bearing field — the *server* decides whether the browser needs a token, so a laptop with no `ADMIN_API_KEY` never asks for one |

Writes: **none.**

---

## 1. Drafting a question — `POST /questions/draft`, `POST /questions/critique`

```
NewForecastView  "Draft with AI"
   text (≥20 chars) ──► POST /questions/draft { text }
                          run_draft(text)      ← agent call 1
                          run_critique(...)    ← agent call 2
                        ◄── { parsed: {question, criteria, date, source, category},
                              critique: {is_resolvable, ambiguities, missing,
                                         suggested_criteria, suggested_resolution_source} }
   setFields(parsed); setCritique(critique); phase → "review"
```

| Layer | Detail |
|---|---|
| FE | `NewForecastView.jsx:20` — spinner, no streaming |
| API | `questions.py:37` — public, **stateless**, two sequential agent calls |
| DB | **nothing written.** The draft lives in React state until you press a save button |
| Errors | parse `AgentTimeout` → **504** (you get your text back); critique degrades silently rather than raising |

`POST /questions/critique` is the same critique half exposed alone — P3 resolvability review of
a question + criteria, also stateless, also writing nothing. The React app does not call it;
`api.js` has no wrapper. It exists for the CLI (`superforecaster critique`) and scripts.

---

## 2. Creating and starting a run — `POST /runs`, `PATCH /runs/{id}`, `POST /runs/{id}/start`

Both buttons in `NewForecastView.save()` write; only "Run now" also starts.

```
"Add to backlog"                     "Run now"
  POST /runs {fields}                  POST /runs {fields}
        │                                    │
        └─ db.create_gated_run               ├─ db.create_gated_run
           INSERT gated_runs                 │  INSERT gated_runs (status='backlog')
           status='backlog'                  │
                                             └─ POST /runs/{id}/start   [admin]
                                                machine.start_run
                                                  ├─ four-field gate → 422 if missing
                                                  ├─ db.start_gated_run  UPDATE status='active'
                                                  ├─ db.insert_steps     INSERT 1 row: decompose/pending
                                                  └─ machine.detail(id)  → GatedRunDetail
```

| Layer | Detail |
|---|---|
| Gate | `machine.py:46 REQUIRED_FIELDS` — question, criteria, source, date. Checked at **start**, not create: a half-formed backlog idea is legitimate; a forecast nobody can adjudicate is a wasted search budget |
| CAS | `start_gated_run` is `UPDATE … WHERE status='backlog'`; `rowcount == 0` → `StateError` → 409 |
| Back | the full run detail, `steps: [{stage:"decompose", status:"pending", payload:null}]` |
| Key point | **nothing executes here.** 202 Accepted, one pending row, zero API spend |

`BacklogView` uses the same two calls in the other order: `PATCH /runs/{id}` (save the edits)
then `POST /start`. `PATCH` → `db.update_gated_run_fields`, which refuses once
`status != 'backlog'` (409) — a run's question can't change under a decomposition that already
read it.

---

## 3. Running one step — `POST /runs/{id}/steps/{step_id}/stream`

This is the only endpoint that spends money, and the only one that streams. Per **ADR 46 the
connection *is* the step**: no background task, no registry, no replay buffer.

### 3a. Preflight — before a single SSE byte

`runs.py:152-166`, all synchronous DB reads:

| Check | Fails with |
|---|---|
| step exists and belongs to run | 404 |
| run exists | 404 |
| run is `active` | 409 |
| step is `pending` or `error` (`error` = retryable) | 409 |
| `machine.gate_offender` — every earlier stage complete | 409 |
| `machine.busy()` — `_slot` lock free | 409 |

These duplicate checks inside `execute_step`; the copies exist so the client gets a **status
code**. Once the SSE response starts, the status is already committed to 200.

### 3b. The live path

```
LensCard/StepControls onStart
  └─ useStepStream.start(runId, stepId, {maxIterations})
       AbortController; fetch POST …/stream  (not EventSource — it would auto-reconnect
                                              and silently re-run a cancelled step)
                    │
FastAPI  stream_step ── preflight ──► generate()
                                        task = create_task(work())
                                        loop: await queue.get() → yield "data: {...}"
                                                  ▲
             machine.execute_step ──────────────┐ │
               _slot.acquire()  (one at a time) │ │ emit() — sync, put_nowait,
               db.claim_step                    │ │ never blocks the agent
                 UPDATE run_steps               │ │
                   status='running',            │ │
                   attempts += 1, error=NULL    │ │
                 UPDATE gated_runs error=NULL   │ │
               logfire.span("step {stage}")     │ │
               asyncio.timeout(STAGE_TIMEOUT)   │ │
               _dispatch → stages.run_*_stage ──┘─┘
                            observability.py:197 turns agent events into frames
```

| Frame | Emitted from | Front-end effect (`useStepStream.js:39`) |
|---|---|---|
| `thought` | `PartDeltaEvent.content_delta` | appends to `thoughts`, tail-clipped to 4000 chars |
| `query` | `FunctionToolCallEvent` | replaces the one-line `"tool: query"` |
| `source` | diff of `deps.sources_seen` after each tool result | pushes a source chip |
| `exhausted` | `outside_view.exhausted_notice` | "search budget exhausted — wrapping up" |
| `result` | `work()` after `finish_step` | (not consumed — `run` supersedes it) |
| `run` | `machine.detail(run_id)` | `setRun(payload)`, and `start` resolves to it — whole tree swap |
| `error` | any exception, via `_failure_hint` | red banner on the card |

`api.streamStep` splits the byte stream on `/\r?\n\r?\n/` and flushes whatever is still
buffered when the reader finishes. `sse_starlette` writes **CRLF**, so a split on `"\n\n"`
matches nothing — every frame in this table was silently discarded until that was fixed, and
the UI only ever updated from the refetch `onDone` does.

`emit` must be synchronous — it is called from inside the agent's event handler, where an
`await` would stall token delivery. The `asyncio.Queue` is the adapter from that push-based
sync callback to the pull-based async generator; `work()` is a separate task so the generator
can keep yielding for the minutes the agent runs. Ping every 15s.

### 3c. What lands in the database

| Outcome | Writes |
|---|---|
| Success (non-synthesis) | `finish_step`: `run_steps.payload_json = <model_dump_json>`, `status='complete'` → then `machine.reconcile(run_id)` materializes the next stage's **pending** rows |
| Success (synthesis) | `finish_step`, then `db.save_forecast` (new row in `forecasts`, first row in `forecast_updates`), then `complete_gated_run`: `status='complete'`, `forecast_id`, `error=NULL` |
| Agent/stage failure | `fail_step`: `run_steps.status='error'`, `error=<msg>`; mirrored to `gated_runs.error` |
| Client disconnects | generator `finally` → `task.cancel()` → `CancelledError` inside `execute_step` → `fail_step(step_id, "cancelled")` → **immediately re-claimable** |
| Process restart mid-step | `init_db` → `mark_interrupted_steps` flips `running` → `error='interrupted by restart'` |

`reconcile` (`machine.py`) is the fan-out. It calls `expected_steps`, which reads the payloads
just written and returns the identities `(stage, sub_claim_id, lens_name)` they imply:

```
decompose ─► one lenses step per researchable sub-claim
             (none researchable → straight to synthesis)
lenses    ─► one base_rates step per (sub-claim, lens)
base_rates─► one inside_view step per same cell
inside_view► one synthesis step
```

`expected_steps` stops at the first stage that is not fully complete, so a stage's rows never
appear before the stage that fans them out has finished. `reconcile` then makes the table
match: `have - want` is deleted, `want - have` is inserted. Going forward `want` only grows,
so this path is pure insert — the deletion half exists for §3f. `INSERT OR IGNORE` against the
UNIQUE key makes it idempotent: retrying the last cell of a stage must not duplicate the next
stage's rows.

The stage functions carry the methodology's code-stamped invariants as data transforms:
`run_lenses_stage` never receives a rate (populations chosen blind, ADR 40); `run_base_rate_step`
re-stamps the lens identity from the *chosen* lens so a cell cannot re-weight its population
after measuring it; `run_inside_step` requires a measured `BaseRateStepPayload` by signature
(P4 as a call signature); `run_synthesis_stage` computes the anchor and implied probability with
`checks.anchor_from` / `checks.implied_probability` — **arithmetic first, never the model** —
then loops against `checks.run_forecast_checks` with one retry.

### 3d. Back to the user

Two paths, and the second is why the UI is always correct:

1. the `run` frame → `setRun(fullDetail)` mid-stream, so the new pending cards appear the
   instant the step lands;
2. `onDone` → `refreshDetail()` (`GET /runs/{id}`, §4) **plus** `onChanged()` → `GET /runs` for
   the sidebar. Even if the stream died mid-frame, the next paint is rebuilt from SQLite alone.

`RunView` renders purely from `run.steps` — `stepFor(stage, subClaim, lens)` finds the row,
`step.payload` is the parsed stage output, and `derive.js` recomputes the displayed arithmetic
(counted → adjusted → weighted → chain → implied) mirroring `checks.py`. There is no
client-side accumulation of run state beyond the live tail of the one active card.

### 3e. Retry / deeper budget

`StepControls` shows "Retry" when `step.status === 'error'`, and additionally "Retry with 2×
search budget" when `step.error` matches `/budget|max_iterations|UsageLimit/`. That posts the
same URL with `?max_iterations=run.max_iterations * 2`, which becomes
`ForecastInput.max_iterations` for that one call only — it is never persisted onto the run
(ceiling `MAX_SEARCH_DEPTH = 50`).

### 3f. Run All — draining every remaining step

There is no server-side queue. The request *is* the step and its last frame is the updated run
(ADR 46), so Run All is a loop in the browser (ADR 55):

```
RunHeader "Run All"  (starts a backlog run first, via POST /runs/{id}/start)
  -> useRunQueue.drain(scope, run)
       -> runQueue.nextRunnable(run, scope) -> step | null    [mirrors db.STAGE_ORDER + the gate]
       -> useStepStream.start(runId, stepId) -> run | null    [the same §3 stream]
       -> repeat with the returned run, until nextRunnable is null
```

`start` resolves to the run its `run` frame carried, or `null` if the step failed — a failure
emits an `error` frame and no `run` frame, so the loop has nothing to continue from and halts.
Run Section is the same loop with `scope` set to one stage. **Stop** calls `stream.abort()`,
which disconnects, which cancels the step server-side exactly as closing the tab does.

`stream.streaming` (a request is in flight) is separate from `stream.active` (there is a card
to draw, error included). Every Run and Retry button keys off `streaming`.

---

## 3g. Editing a payload — `PUT /runs/{id}/steps/{step_id}/payload`

Admin-only. Correct a decomposition or a lens set before anything is researched against it.

```
PUT /runs/{run_id}/steps/{step_id}/payload   {edited payload}
  -> machine.edit_payload(run_id, step_id, body)
       -> db.get_step / db.get_gated_run                 404, or 409 if run is not active
       -> machine.edit_blocker(step, steps) -> None | str            [409 when set]
       -> Decomposition | SubClaimLensesEdit .model_validate(body)   [422 on reject]
       -> agents.decompose.with_ids(payload)             decompose only: re-stamp sc1…scN
       -> db.edit_step_payload(step_id, payload_json)    [writes: run_steps.payload_json, edited_at]
       -> machine.reconcile(run_id)                      [writes: run_steps — deletes and inserts]
  -> machine.detail(run_id)
  -> 200 {the whole run, steps included}
```

`edit_blocker` reads `machine.DERIVED`: `decompose` derives the `lenses` rows and `synthesis`;
`lenses` derives **every** `base_rates` row in the run, not only its own sub-claim's. A payload
is editable while every derived row is still `pending`, so an edit can only ever strand empty
rows — which is why `reconcile`'s delete half never destroys work. The lens rule is wider than
the delete rule needs because populations are pre-registered (ADR 40): once any rate is back,
re-choosing populations anywhere means choosing them with a measured number in hand. It refuses (`GateError`) if a
stale row is not `pending`, raising before either write.

`status` and `attempts` do not move: the step is still complete, and an edit is not an attempt.
`edited_at` is what distinguishes a payload a person wrote, and surfaces as an "edited" chip.

The frontend mirrors the lock in `derive.editBlocker` so the Edit pencil and the lock chip
agree with what the API will accept, the same way `derive.js` mirrors `checks.py`.

---

## 4. Reload — `GET /runs/{id}`

The whole-tree read, and the reason a refresh never loses anything.

```
RunView mount / refreshDetail()
  └─ GET /runs/{id} → machine.detail(run_id)
                        db.get_gated_run  + db.list_steps
                        payload_json ──json.loads──► RunStepOut.payload
                      ◄── GatedRunDetail { …run fields, steps: [...] }
```

Writes: **none.** Every stage payload was `model_dump_json`'d into its row at `finish_step`, so
the entire reasoning trail — every lens, every evidence block, every adjustment — reconstructs
from SQLite with no agent calls and no in-memory state.

---

## 5. Deleting — `DELETE /runs/{id}`

Reached from **Delete** in the run header (any run) or **Remove** in `BacklogView`. Both open
`ConfirmDialog` first, which names the question and says what is lost — how many steps have
run, and that a saved forecast survives its run. Cancel is focused, and Escape or a click on
the backdrop takes it. On success `App` clears the selection and refreshes the sidebar; on
failure the dialog stays open with the message, so a 401 is retryable rather than fatal.

`db.delete_gated_run` → one `DELETE FROM gated_runs`; `run_steps` go with it via
`ON DELETE CASCADE`. A `forecasts` row produced by that run **survives** (`ON DELETE SET
NULL`) — the published forecast outlives the scaffolding that made it.

---

## 6. Ungated pipeline — `POST /forecasts`

The API twin of `superforecaster forecast`. Not called by the React app.

```
POST /forecasts {question, criteria, date, category, resolution_source, submission_gap_days}
  └─ stages.run_all(ForecastInput)          ← blocking, minutes, all 5 stages back-to-back
       decompose → lenses → base_rates → inside_view → synthesis
       per-stage fan-out = asyncio.gather(return_exceptions=True)  ← a failed cell degrades
     ► (forecast, violations)
  └─ db.save_forecast   INSERT forecasts + INSERT forecast_updates (the initial probability)
  └─ db.get_forecast    ◄── ForecastRecord
```

Same `stages` functions the gated flow uses — one implementation of the methodology, not two
(ADR 45). No gates, no SSE, no `run_steps` rows: nothing to resume if it dies. Runs live, with
no `as_of` or `model` clamp (those exist for backtesting). `violations` is computed and
discarded by this endpoint.

---

## 7. Forecast reads — `GET /forecasts`, `GET /forecasts/{id}`

Pure SELECTs against the published table. Not called by the React app.

| Endpoint | Lineage |
|---|---|
| `GET /forecasts?status=active\|resolved\|ambiguous&limit&offset` | `db.list_forecasts` → `list[ForecastRecord]` |
| `GET /forecasts/{id}` | `db.get_forecast` → `ForecastRecord`, or 404 |

Writes: **none.**

---

## 8. Update, resolve, refresh — `POST /forecasts/{id}/updates`, `PATCH /{id}/resolve`, `POST /{id}/refresh`

The post-publication lifecycle. Not called by the React app.

```
POST /forecasts/{id}/updates {probability, reasoning}     [admin]
  └─ db.add_forecast_update   INSERT forecast_updates; UPDATE forecasts.probability
     404 NotFoundError · 409 StateError (already resolved)

PATCH /forecasts/{id}/resolve {outcome: 0|1|null}          [admin]
  └─ db.resolve_forecast      UPDATE forecasts status='resolved'|'ambiguous',
                                     outcome, resolved_at, brier (scoring.brier_score over
                                     scoring.time_weighted_probability of forecast_updates)
  └─ db.get_forecast        ◄── ForecastRecord

POST /forecasts/{id}/refresh                               [admin]
  └─ graphs.run_update_graph(id)      ← the ONLY graph left
       CheckResolved   run_resolution_check agent
                       db.mark_refreshed(id, flagged=appears_resolved)
                       appears_resolved → End (flagged_resolved=True)   ← short-circuit
       ApplyBayes      run_update agent → P11 likelihoods
       VerifyLargeMove second opinion when the move is large
       GuardUpdate     |Δp| < MIN_PROBABILITY_DELTA → drop as noise
                       else db.add_forecast_update  INSERT forecast_updates
     ◄── RefreshActionResponse {updated, reason}
```

The resolution check runs **before** the probability update and short-circuits on a resolved
forecast, so a resolved question can never be re-forecast. That ordering lives in the graph, not
in the callers — which is why §10's sweep is a plain loop.

---

## 9. Calibration — `GET /calibration`

```
GET /calibration
  └─ db.calibration_report()
       SELECT resolved forecasts
       scoring.brier_score + scoring.calibration (pure functions)
     ◄── CalibrationReport { brier, buckets: [CalibrationBucket] }
```

Writes: **none.** The scoring input is the *time-weighted* probability over `forecast_updates`,
not the latest value — a forecast held at 0.9 for a month scores differently from one moved to
0.9 yesterday.

---

## 10. Cron sweep — `POST /admin/refresh/run`

The same work the scheduler does at `REFRESH_CRON_SCHEDULE` (default 06:00 UTC), triggered by
hand. Not called by the React app.

```
POST /admin/refresh/run                                    [admin]
  └─ cron.run_daily_refresh()
       db.list_active_forecast_ids()
       for each: run_update_graph(fid)          ← §8, writes per forecast
                 exception → summary.errors, continue   ← one bad forecast never kills the sweep
     ◄── RefreshSummary {total_checked, total_updated, total_flagged_for_review,
                         total_skipped, errors}
```

---

## 11. Health & static — `GET /healthz`, `GET /`

| Endpoint | Lineage |
|---|---|
| `GET /healthz` | constant JSON. No DB. The Docker healthcheck |
| `GET /` and all unmatched paths | `StaticFiles(FRONTEND_DIR, html=True)`, **mounted last** in `main.py` so it never shadows an API route. Serves the Vite build |

---

## Auth, in one line

`require_admin` guards `DELETE /runs/{id}`, `POST /runs/{id}/start`, the step stream, every
`/forecasts` write, and `/admin/*`. With `ADMIN_API_KEY` unset **and** the request from loopback
**and** no proxy header, it is skipped entirely (`is_local_mode`). The browser sends
`Authorization: Bearer <token>` from `localStorage.sf_admin_token` when it has one.

---

## Module reference

Where the functions named above live.

**Backend — `backend/superforecaster/`**

| Module | Holds |
|---|---|
| `machine.py` | gated-run state machine: `start_run`, `expected_steps`, `reconcile`, `gate_offender`, `execute_step`, `edit_blocker`, `edit_payload`, `detail`, `busy`, `DERIVED`, `REQUIRED_FIELDS`, `GateError`, `BusyError` |
| `stages.py` | `run_decompose_stage`, `run_lenses_stage`, `run_base_rate_step`, `run_inside_step`, `assemble_outside`, `run_synthesis_stage`, `run_all`, `normalize_weights` |
| `db.py` | SQLite (WAL, FKs, migrations). Forecast fns + gated-run fns + `NotFoundError`/`StateError` |
| `models.py` | every Pydantic model |
| `checks.py` | the 16 principles as pure functions; `lens_rate`, `anchor_from`, `implied_probability`, `run_forecast_checks`, `blocking` |
| `scoring.py` | `time_weighted_probability`, `brier_score`, `calibration` — pure |
| `deps.py` | `ForecastDeps`, `SearchBudget`, `sources_seen`, the `emit` sink |
| `tools.py` | `search_web` (Tavily), `search_wikipedia`, `as_of` backdating clamps |
| `observability.py` | logfire config, the `run_agent` wrapper, agent-event → stream-frame handler |
| `errors.py` | `AgentTimeout`, `StageTimeout` |
| `cron.py` | `run_daily_refresh`, APScheduler wiring |
| `model_garden.py` | model registry with published training cutoffs (backtest clamp) |
| `graphs/update.py` | the only graph: `CheckResolved → ApplyBayes → VerifyLargeMove → GuardUpdate`, `run_update_graph` |
| `agents/` | eleven agents, one module each: `decompose`, `lenses`, `outside_view`, `inside_view`, `reflect`, `synthesize`, `critic`, `draft`, `resolution`, `update`, `postmortem` |
| `evals/components.py` | per-agent eval harness (`run_component`, `SCORERS`) |
| `__main__.py` | typer CLI: `forecast`, `refresh`, `resolve`, `critique`, `postmortem`, `models`, `diagram`, `test`, `config`, `serve` |

**Backend — `backend/api/`**

| Module | Holds |
|---|---|
| `main.py` | FastAPI app, startup preflight, `/healthz`, `/config`, static mount (last) |
| `deps.py` | `require_admin`, `is_local_mode` |
| `runs.py` | gated-run CRUD + `stream_step` + `edit_step_payload` |
| `questions.py` | `/questions/draft`, `/questions/critique` |
| `forecasts.py` | forecast reads + admin writes + single refresh |
| `calibration.py` | `/calibration` |
| `admin.py` | `/admin/refresh/run` |

**Frontend — `frontend/src/`**

| Module | Holds |
|---|---|
| `api.js` | fetch wrappers + `streamStep` (fetch + ReadableStream + AbortController, deliberately not `EventSource`) |
| `runQueue.js` | `STAGE_ORDER`, `nextRunnable`, `sectionRunnable` — mirrors `db.STAGE_ORDER` and the gate. Pure |
| `derive.js` | mirrors `checks.py`: `lensRate`, `adjustedLensRate`, `subClaimRate`, `signedAdjustment`, `claimSupport`; mirrors `machine`/`stages`: `editBlocker`, `normalizeWeights`, `weightSum` |
| `hooks/useRuns.js` | the sidebar list |
| `hooks/useStepStream.js` | one stream at a time; unmount aborts, abort cancels server-side. `start` resolves to the run the stream produced; `streaming` is separate from `active` |
| `hooks/useRunQueue.js` | Run All / Run Section: `drain`, `stop`. A browser loop, no server queue (ADR 55) |
| `App.jsx` | shell + selection model (`new` \| run id); theme toggle; `/config`-driven chips |
| `components/` | `Sidebar`, `NewForecastView`, `BacklogView`, `RunView`, `RunHeader`, `FieldEditor`, `EditorField`, `StepControls`, `LensCard`, `CellActivity`, `LiveTail`, `SynthesisSection`, `DecomposeEditor`, `LensSetEditor`, `ConfirmDialog` |

---

## What actually works

- The full gated flow end to end: draft-from-text → backlog → four-field start gate →
  decompose → per-sub-question lenses → per-cell base rates → per-cell inside view →
  synthesis with the checks loop → saved forecast; every step user-gated, every payload
  persisted, reload-safe.
- **Run All / Run Section** — one click drains every remaining step, or one stage, as a
  browser loop over the same stream endpoint (§3f). Stop cancels the step in flight. A
  failure halts the queue and leaves every button live; clicking Run All again resumes
  from it.
- **Editable review** — a decomposition or a lens set can be corrected while everything
  derived from it is still pending (§3g). One measured base rate locks every lens set,
  lens weights must sum to 1.00, and an edited payload carries an "edited" chip.
- Retry of any failed step (optionally with `?max_iterations=` up to 50); cancel by
  closing the tab; restart sweep marks orphans honestly.
- The CLI pipeline (`forecast`) and component evals through the same `stages` functions.
- Daily refresh cron + manual refresh through the update graph; resolution + scoring +
  calibration report.
- Local mode: two exported keys and `serve`, no token, no `.env`, no build step for the
  API (UI needs `npm run build` once).
- CI on push/PR; opt-in pre-push test gate. 318 backend tests pass with no network and no
  API keys (see the `.env` caveat in Known Issues).
- Nothing is hosted. `docker-compose.yml` runs the API with a named SQLite volume and the
  frontend built into the image; `.github/workflows/ci.yml` is the only automation.

## Known issues

- **A permanently failing cell blocks its run** — strict gating has no skip-a-cell escape
  hatch; retry (optionally deeper) is the only recovery today (deferred in spec5).
- The end-to-end backtest (`test e2e`) is specified in `spec/planned/spec6.md` and not
  built; `superforecaster test e2e` says so and exits 2.
- `GoldenQuestion`, `QuestionScore`, and `Scorecard` are defined in `models.py` but unused —
  the eval corpus they describe does not exist yet (spec6).
- **The test suite is not isolated from `backend/.env`.** `config` loads that file, so a dev
  machine with `ADMIN_API_KEY` set there flips `is_local_mode` to False and the 10 admin-route
  tests in `test_api_gated_runs.py` fail with 401 (surfacing as `KeyError: 'steps'`). CI has no
  `.env` so it stays green — the failure only ever hits a developer. Workaround:
  `ADMIN_API_KEY="" uv run pytest`. The fix is a conftest fixture that pins the admin key.
- **`Forecast.decompositions[].probability` is carried by the model, not computed.**
  `run_synthesis_stage` hands the synthesis agent the decomposition JSON — pre-research
  working estimates included — and instructs it to carry the sub-claims through.
  `check_linkage` verifies the ids survive; nothing compares the probabilities to
  `checks.chain_inputs`. So a saved forecast can show a pre-research guess against a
  sub-question that was actually measured. The fix mirrors `ResearchedLens`, which has no
  `base_rate` field because the rate is computed from evidence. Own spec, not yet written.
- **Old lens weights do not sum to 1.** Runs completed before ADR 54 keep their raw
  relative weights. Every consumer divides by Σw, so no number is wrong — only the
  displayed weights of an old run fail to add up.
- **No frontend test runner.** CI runs `npm run build` only, so pure frontend logic has no
  automated cover at all. This is not theoretical: the SSE frame parser split on the wrong
  line ending and dropped every frame the server sent, from spec5 until spec7, and a build
  cannot catch that. `runQueue.js`, `derive.js`, and `streamStep`'s parser are all pure and
  would be testable the day a runner exists.
