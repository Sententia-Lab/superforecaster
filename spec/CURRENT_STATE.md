# Current State — Data Lineage

**This document has one job: trace every byte from a user action, through each function that
touches it, into SQLite, and back to the screen.** If you want to know where a number came
from or where a click ends up, it is in here.

Deliberately not here:
- **Repository layout** — read the tree.
- **How to run** — `README.md`.
- **Data models** — `backend/superforecaster/models.py`.
- **Environment variables** — `backend/.env.example`, `backend/superforecaster/config.py`,
  and `backend/app/config.py`.
- **Dependencies** — `backend/pyproject.toml`.
- **Why it is shaped this way** — `spec/ADR.md`.
- **The 16 principles** (`P<n>` below) — `spec/superforecasting_methodology.md`.

Last regenerated: 2026-08-17.

---

## Storage map

Everything persists in four SQLite tables (`backend/app/db.py`, `SCHEMA_VERSION = 4`).

| Table | One row means | Written by |
|---|---|---|
| `gated_runs` | one forecast question + its lifecycle | `create/update/start/complete/delete_gated_run` |
| `run_steps` | one stage cell (`UNIQUE(run_id, stage, sub_question_id, lens_name)`) | `insert/claim/finish/fail/delete_steps`, `edit_step_payload` |
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

All 22 routes, each traced in the section named.

| Method + path | Auth | Traced in |
|---|---|---|
| `GET /config` | — | §0 App boot |
| `PUT /config/keys` | admin | §0b Setting a key |
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
  ├─ api.config()      GET /config  → { auth_required, search_enabled, model, keys }
  └─ useRuns.refresh() GET /runs    → [ GatedRunSummary + stage_counts ]
```

```json
{"auth_required": true,
 "search_enabled": true,
 "model": "gateway/anthropic:claude-sonnet-4-6",
 "keys": {"llm": ".env", "llm_var": "PYDANTIC_AI_GATEWAY_API_KEY",
          "tavily": ".env", "wikipedia": "unset"}}
```

| Layer | What happens |
|---|---|
| FE | `App.jsx` sets `config`; `useRuns.js:9` fills the sidebar; `KeyPanel.jsx` reads `keys` |
| API | `main.py _client_config` → `is_local_mode(request)` (`deps.py:15`), `app.config.origin` per key |
| API | `runs.py:60 list_runs` → `db.list_gated_runs` |
| DB | two SELECTs: all runs newest-first, plus a `GROUP BY run_id, stage, status` count roll-up |
| Back | `auth_required` is the load-bearing field — the *server* decides whether the browser needs a token, so a laptop with no `ADMIN_API_KEY` never asks for one |

`keys` values are `environment` / `.env` / `session` / `unset` — **where** each key came
from, never what it is. `llm_var` names the variable credentialing the model, which is
`PYDANTIC_AI_GATEWAY_API_KEY` when the gateway is configured and `ANTHROPIC_API_KEY`
otherwise (`superforecaster.config.active_llm_key_name`).

Writes: **none.**

---

## 0b. Setting a key — `PUT /config/keys`

```
KeyPanel.jsx save()  ->  api.setKeys(body)
```

```json
{"tavily_api_key": "tvly-abc123"}
```

```
  -> require_admin(request)                  [403 without the bearer token, skipped locally]
  -> app.config.set_runtime_key("TAVILY_API_KEY", "tvly-abc123")
       name must be in app.config.RUNTIME_KEYS   [422 otherwise]
       os.environ[name] = value              [writes: process environment only]
  -> _client_config(request)
```

```json
{"auth_required": true, "search_enabled": true,
 "model": "gateway/anthropic:claude-sonnet-4-6",
 "keys": {"llm": ".env", "llm_var": "PYDANTIC_AI_GATEWAY_API_KEY",
          "tavily": "session", "wikipedia": "unset"}}
```

| Layer | What happens |
|---|---|
| FE | `KeyPanel.jsx` sends only fields that were typed; `""` clears one; the admin token goes to `localStorage` via `api.setToken` and never into the body |
| API | `main.py set_keys` maps `llm_api_key` onto `active_llm_key_name()`, so the row always writes the key the next run actually uses |
| Back | `App.jsx setConfig(resp)` — the `no web search` chip clears with no reload |

**Nothing is written to disk.** `get_settings()` re-reads `os.environ` on every call, so the
next agent call picks the key up with no cache to invalidate and no restart; a restart drops
it. `backend/.env` remains the only durable home (ADR 61).

Writes: **the process environment.** No table.

---

## 1. Drafting a question — `POST /questions/draft`, `POST /questions/critique`

Two independent one-call endpoints, each behind its own button. Neither writes anything.

**a. "Draft with AI" — freeform text into four filled fields.**

```
NewForecastView  "Draft with AI"
   text (≥20 chars) ──► POST /questions/draft { text }
                          run_draft(text)      ← the only agent call, no tools
   ◄── { "question": "Will UK CPI inflation exceed 3% in any month of 2027?",
         "resolution_criteria": "Yes if a 2027 month prints above 3.0% year on year.",
         "resolution_date": "2028-01-31T00:00:00Z",
         "resolution_source": "ONS Consumer Price Inflation bulletin",
         "category": "economics" }
   setFields(parsed); phase → "review"
```

All four fields come back filled, so `isComplete(fields)` passes and **"Run now" is live
immediately**. `resolution_source` is named by the agent whether or not the text named
one — it is required on `DraftedQuestion`, so an output missing it is a validation error
pydantic-ai retries (ADR 64). The draft budget allows no tool calls; the name is checked
only if the reader presses "Check resolvable", which does search.

**b. "Check resolvable" — the fields are rewritten in place.**

```
NewForecastView  "Check resolvable"   (needs question + criteria)
   ──► POST /questions/critique { question, resolution_criteria, resolution_date,
                                  resolution_source }
         run_critique(...)  →  _require_a_source(...)
   ◄── { "is_resolvable": false,
         "what_changed": "Replaced 'above 3.0%' with the exact series and vintage, and
                          named the ONS bulletin as the adjudicator.",
         "suggested_criteria": "Yes if the ONS CPIH-excluding-owner-occupiers 12-month
                                rate for any month Jan–Dec 2027 is at or above 3.1%, on
                                first publication.",
         "suggested_resolution_source": "ONS Consumer Price Inflation bulletin" }

   fields.resolution_criteria  ← suggested_criteria         (overwritten)
   fields.resolution_source    ← suggested_resolution_source (overwritten)
   what_changed rendered under the button
```

The rewrite replaces the text rather than offering itself. There is no accept step and
nothing to dismiss — the previous wording is one undo away in the textarea. An empty
suggestion leaves the field alone. `is_resolvable` is not rendered; the API and the CLI
read it, the editor reads the two rewrites.

**Every field the rewrite lands on is a field the request carries**, `resolution_source`
included. The critic judges the author's adjudicator first and returns it unchanged when
it settles the question; it replaces the wording only when that source cannot. An empty
`suggested_resolution_source` still means "no adjudicator" and `_require_a_source` flips
`is_resolvable` — agreeing with the author is expressed by echoing their text, never by
returning nothing. The CLI's `critique` command sends no source (it has no `--source`
option), so it always takes the "(none given)" branch.

The check is optional. A drafted question is already runnable; this sharpens it and
verifies the adjudicator with a live search.

| Layer | Detail |
|---|---|
| FE | `NewForecastView.jsx:24` draft, `:42` check — spinners, no streaming |
| API | `questions.py:20` critique, `:39` draft — public, **stateless**, one agent call each |
| DB | **nothing written.** The draft lives in React state until you press a save button |
| Errors | draft `AgentTimeout` → **504** (you get your text back); the critique degrades to "Nothing changed" with your own criteria handed back, never raising |

`_require_a_source` (`agents/critic.py:139`) forces `is_resolvable=False` and appends the
gap to `what_changed` when the critic named no source. `POST /runs` refuses an empty
`resolution_source` regardless (ADR 44).

`POST /questions/critique` also serves the CLI (`superforecaster critique`) and scripts.

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
BaseRateCard | ModifierCard | StepControls onStart
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
               runner._make_event_handler emits typed events;
               app/stream.py:16 turns each into a frame
```

| Frame | Emitted from | Front-end effect (`useStepStream.js:39`) |
|---|---|---|
| `thought` | `PartDeltaEvent.content_delta` | appends to `thoughts`, tail-clipped to 4000 chars |
| `query` | `FunctionToolCallEvent` | replaces the one-line `"tool: query"` |
| `source` | diff of `deps.sources_seen` after each tool result | pushes a source chip |
| `exhausted` | `outside_view.exhausted_notice` | "search budget exhausted — wrapping up". Payload `{id}` |
| `result` | `work()` after `finish_step` | (not consumed — `run` supersedes it) |
| `run` | `machine.detail(run_id)` | `setRun(payload)`, and `start` resolves to it — whole tree swap |
| `error` | any exception, via `_failure_hint` | sets `failure = {stepId, message}`, which outlives the request — the card shows the message next to its Retry button |

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
just written and returns the identities `(stage, sub_question_id, lens_name)` they imply:

```
decompose ─► one lenses step per researchable sub-question
             (none researchable → straight to synthesis)
lenses    ─► one base_rates step per (sub-question, lens)
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
after measuring it — a cell that cannot measure its population reports that and grades itself
`low` rather than measuring a different one (ADR 72); `run_inside_step` requires a measured `BaseRateStepPayload` by signature
(P4 as a call signature); `run_synthesis_stage` computes the anchor and implied probability with
`checks.anchor_from` / `checks.implied_probability` — **arithmetic first, never the model** —
then loops against `checks.run_forecast_checks` with one retry.

`anchor_from` and `implied_probability` both reach `checks.combine_sub_question_rates(rates,
chain_rule, dependent_groups)`, the only place the chain is applied. Sub-questions named by a
`DependentGroup` combine first under `models.DEPENDENCE[kind]` — 0.0, 0.35 or 0.50 — which
slides the group from independent toward `min(rates)` for a conjunction or `max(rates)` for a
disjunction. The group values and the ungrouped rates then combine as independent. An empty
`dependent_groups` is the plain product, which is what every run produces unless the decompose
agent or a hand edit names a group (ADR 65).

### 3d. Back to the user

Two paths, and the second is why the UI is always correct:

1. the `run` frame → `setRun(fullDetail)` mid-stream, so the new pending cards appear the
   instant the step lands;
2. `onDone` → `refreshDetail()` (`GET /runs/{id}`, §4) **plus** `onChanged()` → `GET /runs` for
   the sidebar. Even if the stream died mid-frame, the next paint is rebuilt from SQLite alone.

`RunView` renders purely from `run.steps` — `stepFor(stage, subQuestion, lens)` finds the row,
`step.payload` is the parsed stage output, and `derive.js` recomputes the per-lens arithmetic
(counted → adjusted → weighted) mirroring `checks.py`. It stops at `subQuestionRate`: the
chain and the implied probability are read from the stored `payload.anchor` and
`payload.implied`, never recomputed in JavaScript. There is no client-side accumulation of
run state beyond the live tail of the one active card.

**What the tree draws.** Every stage is a `<details>` (`StageSection`), open while the run is
in flight and collapsed once synthesis completes — at which point section 5 renders *above*
section 1, so a finished run opens on its answer. Each stage body opens with a collapsed
`HowThisWorks` panel — static copy naming what the stage produces and which tools its agent
holds, keyed by stage, stored nowhere. Section 2 gives each lens a `LensCard` — the same
nested card a measured cell gets — carrying `population`, `why_it_fits` and
`weight_rationale` in three labelled blocks; those last two appear nowhere
else. Sections 3 and 4 group their cells into one card per sub-question, holding
`BaseRateCard`s and `ModifierCard`s respectively. Both restate the lens in a `LensOrigin`
panel, so lens output stays visibly separate from the cell's own analysis; the base-rate card
hides each `Evidence.note` behind *How this was counted* and the disagreement behind its own
accordion, and the modifier card leads each move with its signed magnitude and
`Adjustment.title` (falling back to the first sentence of `evidence` for payloads written
before that field). Display labels — `Sub-question 1`, `Lens 1`, `Base rate 1`, `Modifier 1` —
are computed from position in `labels.js` and never stored (ADR 59).

Both cell cards end in a `SourceList` — the `sources` their payload stored, one row per URL
with the search that returned it. The stream's `source` chips die with the request; these are
read back from `run_steps.payload_json`, so a reloaded run still shows what each cell read.

**Prose.** Every agent-written string renders through `Prose.jsx` (`react-markdown` +
`remark-gfm`), which is also what turns a bare URL into a link (ADR 60).

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
       -> runQueue.nextRunnable(run, scope) -> step | null    [mirrors stages.STAGE_ORDER + the gate]
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
       -> Decomposition | SubQuestionLensesEdit .model_validate(body)   [422 on reject]
            Decomposition rejects a `dependent_groups` entry naming a position that does
            not exist, a position in two groups, or any group under a `custom` chain rule
       -> agents.decompose.with_ids(payload)             decompose only: re-stamp sq1…sqN
            `model_copy(update={"sub_questions": …})`, so `dependent_groups` survives —
            it names members by position, which is what this re-stamps ids from
       -> db.edit_step_payload(step_id, payload_json)    [writes: run_steps.payload_json, edited_at]
       -> machine.reconcile(run_id)                      [writes: run_steps — deletes and inserts]
  -> machine.detail(run_id)
  -> 200 {the whole run, steps included}
```

`edit_blocker` reads `machine.DERIVED`: `decompose` derives the `lenses` rows and `synthesis`;
`lenses` derives **every** `base_rates` row in the run, not only its own sub-question's. A payload
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

`SynthesisSection` renders the whole derivation as one table: a spanning header row per
sub-question carrying the question text, its lens rows, one row per modifier beneath its lens
(`derive.adjustmentsForLens`), and a `Blended` footer per sub-question. It scrolls inside its
own container rather than widening the page.

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
  └─ app.update.run_update_graph(id)
       db.get_forecast(id)                 → None means "not found", decided here
       superforecaster.update.run_update_cycle(record, deps)   ← no storage below this line
         CheckResolved   run_resolution_check agent
                         appears_resolved → End (flagged_resolved=True)  ← short-circuit
         ApplyBayes      run_update agent → P11 likelihoods
         VerifyLargeMove second opinion when the move is large
         GuardUpdate     |Δp| < MIN_PROBABILITY_DELTA → drop as noise
                         else End(updated=True, new_probability, reasoning)
       db.mark_refreshed(id, flagged=outcome.flagged_resolved)   UPDATE forecasts
       outcome.updated → db.add_forecast_update  INSERT forecast_updates
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

`require_admin` guards `DELETE /runs/{id}`, `POST /runs/{id}/start`, the step stream,
`PUT /config/keys`, every `/forecasts` write, and `/admin/*`. With `ADMIN_API_KEY` unset **and** the request from loopback
**and** no proxy header, it is skipped entirely (`is_local_mode`). The browser sends
`Authorization: Bearer <token>` from `localStorage.sf_admin_token` when it has one.

---

## Module reference

Where the functions named above live.

Imports run one way: `api -> app -> superforecaster` (ADR 73).

**Backend — `backend/superforecaster/`** — the library. No SQLite, no HTTP, no CLI, no
scheduler, and no side effects on import.

| Module | Holds |
|---|---|
| `__init__.py` | the public surface: `ForecastInput`, `ForecastDeps`, `Forecast`, `run_all`, `__version__` |
| `config.py` | `Budget`/`BUDGETS`/`get_budget`, `CheckThresholds`/`get_check_thresholds`, `Settings`/`get_settings`, `resolve_agent_model`, `get_model_settings`, the two timeouts. Reads `os.environ`; never loads a file |
| `stages.py` | `STAGE_ORDER`, `run_decompose_stage`, `run_lenses_stage`, `run_base_rate_step`, `run_inside_step`, `assemble_outside`, `run_synthesis_stage`, `run_all`, `normalize_weights` |
| `update.py` | the update cycle: `CheckResolved → ApplyBayes → VerifyLargeMove → GuardUpdate`, `run_update_cycle`, `UpdateState`, `update_mermaid`. Returns an `UpdateOutcome`; writes nothing |
| `models.py` | every Pydantic model |
| `checks.py` | the 16 principles as pure functions; `lens_rate`, `anchor_from`, `implied_probability`, `combine_sub_question_rates`, `run_forecast_checks`, `blocking` |
| `scoring.py` | `time_weighted_probability`, `brier_score`, `calibration` — pure |
| `deps.py` | `ForecastDeps` — the `as_of`/`model` clamps, the column tag, the run's `Budget`, `sources_seen`, the `emit` sink |
| `events.py` | `Query`, `Source`, `Thought`, `Exhausted`, and the `Sink` type `deps.emit` takes |
| `runner.py` | `run_agent` — attaches the budget, applies the deadline, opens the span, translates timeouts. Emits Logfire spans; never configures Logfire |
| `tools.py` | `search_web` (Tavily), `search_wikipedia` (optional bearer key), `as_of` backdating clamps |
| `errors.py` | `AgentTimeout`, `StageTimeout` |
| `model_garden.py` | model registry with published training cutoffs (backtest clamp). Reads its bundled JSON; never writes it |
| `agents/` | eleven agents, one module each: `decompose`, `lenses`, `outside_view`, `inside_view`, `reflect`, `synthesize`, `critic`, `draft`, `resolution`, `update`, `postmortem`. `__init__.py` holds `attach_budget` — the per-iteration budget instruction, in three bands so the order to stop arrives while a search is still legal (ADR 68) — `withdraw_spent_tools`, which stops offering the search tools once the budget is spent (ADR 69), plus `SEARCH_RESERVE` and `spent_usd` |

**Backend — `backend/app/`** — everything that runs it.

| Module | Holds |
|---|---|
| `config.py` | `load_env`, `ENV_FILE`, `AppSettings`/`get_app_settings` (database path, admin key, cron schedule, frontend dir), `set_runtime_key`, `origin`, `RUNTIME_KEYS` |
| `db.py` | SQLite (WAL, FKs, migrations). Forecast fns + gated-run fns + `NotFoundError`/`StateError` |
| `machine.py` | gated-run state machine: `start_run`, `expected_steps`, `reconcile`, `gate_offender`, `execute_step`, `edit_blocker`, `edit_payload`, `detail`, `busy`, `DERIVED`, `REQUIRED_FIELDS`, `GateError`, `BusyError` |
| `update.py` | `run_update_graph` — loads the record, runs the core cycle, writes the result |
| `cron.py` | `run_daily_refresh`, APScheduler wiring |
| `observability.py` | `configure_logfire`, the write-token probe, `cloud_tracing_active`, `console_active` |
| `stream.py` | `frame` — an `AgentEvent` as the `{type, sub_question, payload}` the browser reads |
| `cli.py` | typer CLI (`superforecaster` console script): `forecast`, `refresh`, `resolve`, `critique`, `postmortem`, `models`, `diagram`, `test`, `config`, `serve` |
| `fixtures/` | the three JSON questions `--fixture` loads |
| `evals/components.py` | per-agent eval harness (`run_component`, `SCORERS`). Scorers only — the case files are not written yet |
| `evals/decompose_eval.py` | pydantic-evals dataset for the decompose agent: three mechanical evaluators plus an `LLMJudge` scoring against `RUBRIC`. `--model`, `--judge-model`, `--budget` flags. Run as a script, not under pytest — it calls the real model |
| `evals/eval_agents/trajectory.py` | judges an agent's tool calls, not its output. `record_trajectory` captures one run via `capture_run_messages` and writes it to the case; `ToolTrajectoryJudge` scores tool selection, arguments, and call count with a second model. Counting is left to pydantic-evals' own `MaxToolCalls`/`MaxModelRequests`, which read spans. Agent-neutral — each eval brings its own rubric |
| `evals/critic_eval.py` | pydantic-evals dataset for the critic agent: `Verdict` and `NamedASource` on the output, `MaxToolCalls` (per case) and `MaxModelRequests` on the spans, an `LLMJudge` on `RUBRIC`, and a `ToolTrajectoryJudge` on `TOOL_RUBRIC`. Same flags as `decompose_eval.py`, and the same reason for being a script |

**Backend — `backend/api/`**

| Module | Holds |
|---|---|
| `main.py` | FastAPI app, startup preflight, `/healthz`, `/config`, `PUT /config/keys`, static mount (last) |
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
| `runQueue.js` | `STAGE_ORDER`, `nextRunnable`, `sectionRunnable` — mirrors `stages.STAGE_ORDER` and the gate. Pure |
| `derive.js` | mirrors `checks.py`: `lensRate`, `adjustedLensRate`, `subQuestionRate`, `signedAdjustment`, `claimSupport`; mirrors `machine`/`stages`: `editBlocker`, `normalizeWeights`, `weightSum` |
| `hooks/useRuns.js` | the sidebar list |
| `hooks/useStepStream.js` | one stream at a time; unmount aborts, abort cancels server-side. `start` resolves to the run the stream produced. Three separate states: `active` is work in flight and ends with the request, `failure` holds the last message and outlives it, `streaming` is what buttons disable on |
| `hooks/useRunQueue.js` | Run All / Run Section: `drain`, `stop`. A browser loop, no server queue (ADR 55) |
| `App.jsx` | shell + selection model (`new` \| run id); theme toggle; `/config`-driven chips |
| `labels.js` | `subQuestionLabel`, `ordinal`, `firstSentence` — display labels computed from position, never stored (ADR 59). Also `DEPENDENCE_KINDS` / `dependenceKind`, the label and one-line meaning of each dependence kind |
| `components/` | `Sidebar`, `NewForecastView`, `BacklogView`, `RunView`, `RunHeader`, `FieldEditor`, `EditorField`, `StepControls`, `BaseRateCard`, `ModifierCard`, `SourceList`, `LensCard`, `HowThisWorks`, `LensOrigin`, `Accordion`, `Prose`, `KeyPanel`, `CellActivity`, `LiveTail`, `SynthesisSection`, `DecomposeEditor`, `DependentGroups`, `LensSetEditor`, `ConfirmDialog` |

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
- **Sub-questions that move together** — the decompose agent groups sub-questions that are
  not independent and names the kind of link; the group combines under a dependence
  parameter before the chain rule is applied (ADR 65). Editable per row in the decompose
  editor. A decomposition with no groups produces the plain product, unchanged.
- Retry of any failed step (optionally with `?max_iterations=` up to 50); cancel by
  closing the tab; restart sweep marks orphans honestly.
- **A readable run tree** — every stage collapses, a finished run leads with its answer,
  each cell shows which text came from its lens, long enumerations and steel-man arguments
  sit behind accordions, and the synthesis table lays out every lens and every modifier in
  one place. Agent prose renders as markdown, bare URLs included (ADR 60).
- **The Keys panel** — the admin token plus the LLM, Tavily and Wikipedia keys, settable
  from the header. Server-held keys apply on the next request and are dropped on restart;
  no route ever returns a key value (§0b, ADR 61).
- The CLI pipeline (`forecast`) and component evals through the same `stages` functions.
- Daily refresh cron + manual refresh through the update cycle; resolution + scoring +
  calibration report.
- **`superforecaster` installs and imports on its own** (ADR 73). The wheel carries no
  SQLite layer, no CLI, and no web framework, and importing it opens no socket and
  configures no logging. `tests/test_layering.py` asserts both.
- Local mode: two exported keys and `serve`, no token, no `.env`, no build step for the
  API (UI needs `npm run build` once).
- CI on push/PR; opt-in pre-push test gate. The backend suite passes with no network and
  no API keys.
- Nothing is hosted. `docker-compose.yml` runs the API with a named SQLite volume and the
  frontend built into the image; `.github/workflows/ci.yml` is the only automation.

## Known issues

- **A permanently failing cell blocks its run** — strict gating has no skip-a-cell escape
  hatch; retry (optionally deeper) is the only recovery today (deferred in spec5).
- The end-to-end backtest (`test e2e`) is specified in `spec/planned/spec6.md` and not
  built; `superforecaster test e2e` says so and exits 2.
- `GoldenQuestion`, `QuestionScore`, and `Scorecard` are defined in `models.py` but unused —
  the eval corpus they describe does not exist yet (spec6).
- **The component eval harness has no data.** `app/evals/components.py` holds a working
  scorer for each of eight agents; the case files were deleted rather than kept as eight
  empty arrays, so `superforecaster test component` reports 0 cases for every agent.
- **`Forecast.decompositions[].probability` is carried by the model, not computed.**
  `run_synthesis_stage` hands the synthesis agent the decomposition JSON — pre-research
  working estimates included — and instructs it to carry the sub-questions through.
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
