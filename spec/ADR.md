# Architecture Decision Record

Every architecture decision made on this project, why it was made, and what it ruled out.
Superseded decisions stay in the record with a pointer to what replaced them — that history is
the useful part.

Sources: `spec/implemented/SPEC_04_26_2026.md` (v3), `spec/implemented/spec3.md` (v4),
`spec/planned/spec4.md`, and the sessions that produced them.

| Status | Meaning |
|---|---|
| **Accepted** | In force today |
| **Superseded** | Replaced; see the pointer |
| **Proposed** | Written down, not yet built |

---

## Index

| # | Decision | Status |
|---|---|---|
| [1](#adr-1--pydantic-ai-as-the-agent-framework) | Pydantic AI as the agent framework | Accepted |
| [2](#adr-2--sqlite-for-persistence) | SQLite for persistence | Accepted |
| [3](#adr-3--fastapi-for-the-rest-layer) | FastAPI for the REST layer | Accepted |
| [4](#adr-4--nextjs--mui-for-the-frontend) | Next.js + MUI for the frontend | **Superseded by 24** |
| [5](#adr-5--api-key-auth-for-admin-actions) | API key auth for admin actions | Accepted |
| [6](#adr-6--open-submission-tracked-by-hashed-ip) | Open submission, tracked by hashed IP | Accepted |
| [7](#adr-7--time-weighted-brier-scoring) | Time-weighted Brier scoring | Accepted |
| [8](#adr-8--docker-for-local-dev-and-deployment) | Docker for local dev and deployment | Accepted |
| [9](#adr-9--daily-batch-updates-not-event-driven) | Daily batch updates, not event-driven | Accepted |
| [10](#adr-10--a-single-agent-in-one-structured-call) | A single agent in one structured call | **Superseded by 11** |
| [11](#adr-11--one-agent-per-methodology-step) | One agent per methodology step | Accepted |
| [12](#adr-12--pydantic-graphs-for-orchestration) | Pydantic graphs for orchestration | Accepted |
| [13](#adr-13--methodology-checks-are-pure-functions) | Methodology checks are pure functions | Accepted, amended by 29 |
| [14](#adr-14--every-threshold-is-configuration) | Every threshold is configuration | Accepted |
| [15](#adr-15--no-per-forecast-granularity-check) | No per-forecast granularity check | Accepted |
| [16](#adr-16--a-large-move-is-verified-not-capped) | A large move is verified, not capped | Accepted |
| [17](#adr-17--two-clamps-for-contamination-free-backtesting) | Two clamps for contamination-free backtesting | Accepted |
| [18](#adr-18--pick_clean_model-returns-none-rather-than-falling-back) | `pick_clean_model` returns None rather than falling back | Accepted |
| [19](#adr-19--unit-tests-cover-logic-and-contamination-only) | Unit tests cover logic and contamination only | Accepted |
| [20](#adr-20--component-golden-data-ships-empty) | Component golden data ships empty | Accepted |
| [21](#adr-21--the-end-to-end-backtest-is-deferred) | The end-to-end backtest is deferred | Accepted |
| [22](#adr-22--flat-modules-except-agents-graphs-evals) | Flat modules, except agents / graphs / evals | Accepted |
| [23](#adr-23--spec-lifecycle-planned--implemented) | Spec lifecycle: planned → implemented | Accepted |
| [24](#adr-24--a-zero-build-static-frontend) | A zero-build static frontend | Accepted |
| [25](#adr-25--sse-for-live-runs-not-websockets) | SSE for live runs, not WebSockets | Accepted |
| [26](#adr-26--runs-are-memory-resident-the-trail-is-kept-client-side) | Runs are memory-resident; the trail is kept client-side | Accepted |
| [27](#adr-27--the-ui-projects-typed-state-it-never-asks-for-narration) | The UI projects typed state; it never asks for narration | Accepted |
| [28](#adr-28--a-failed-run-resumes-from-its-last-completed-node) | A failed run resumes from its last completed node | Accepted |
| [29](#adr-29--an-extreme-probability-is-justified-not-forbidden) | An extreme probability is justified, not forbidden | Accepted |

---

## ADR 1 — Pydantic AI as the agent framework

**Status:** Accepted (v3)

**Decision.** Use Pydantic AI. Not LangChain, LlamaIndex, or raw API calls.

**Rationale.** Typed structured output is the core requirement — the agent must return a
`Forecast` object, not freeform text. Pydantic AI does this natively with validation. The
alternatives add abstraction without solving that better.

**Rules out.** Other agent frameworks. This has held through two major rewrites and the
`output_type` guarantee has become load-bearing (see ADR 11).

---

## ADR 2 — SQLite for persistence

**Status:** Accepted (v3)

**Decision.** Forecasts and community questions live in a local SQLite file.

**Rationale.** Zero infrastructure, queryable with SQL, a single `.db` on disk. The schema can
migrate to Postgres later without changing application logic.

**Rules out.** A database server, until SQLite is a demonstrated bottleneck.

---

## ADR 3 — FastAPI for the REST layer

**Status:** Accepted (v3)

**Decision.** FastAPI.

**Rationale.** Native async matches Pydantic AI's model. Auto-generated OpenAPI at `/docs`
matters for a public project. Pydantic models are shared between the agent and API responses,
so there is one definition of a `Forecast`.

**Known cost, largely retired (spec3.1).** `POST /forecasts` still runs a full graph
synchronously inside the request and is kept that way for API clients that want one call and
one answer. `POST /runs` is the async path: it schedules the graph as a background task and
returns 202 in milliseconds, with progress delivered over SSE. See ADR 25 and ADR 26.

---

## ADR 4 — Next.js + MUI for the frontend

**Status:** **Superseded by ADR 24** (v3 → spec3.1)

**Decision was.** Next.js App Router on Vercel, MUI v6, TypeScript throughout. No Tailwind.

**Rationale was.** SSR matters because public forecast pages should be indexable. MUI gives a
consistent component set without building a design system.

**Why it was replaced.** The second half of that rationale expired the moment a design system
arrived. See ADR 24.

---

## ADR 5 — API key auth for admin actions

**Status:** Accepted (v3)

**Decision.** Admin endpoints behind a static Bearer token (`ADMIN_API_KEY`). No OAuth, no
user accounts.

**Rationale.** Single-admin platform. Full OAuth is unnecessary complexity.

---

## ADR 6 — Open submission, tracked by hashed IP

**Status:** Accepted (v3)

**Decision.** Anyone can submit or vote without an account. Rate limiting and vote
deduplication key off a SHA-256 hash of the IP. Raw IPs are never stored.

**Rationale.** Friction kills community contribution; the admin review step is the quality
gate, not registration. Hashing preserves deduplication without storing personal data.

**Vote semantics.** ±1, switchable and undoable. Questions sort by net score.

---

## ADR 7 — Time-weighted Brier scoring

**Status:** Accepted (v3)

**Decision.**

```
scored_probability = Σ(probability_i × duration_i) / total_duration
brier_score        = (scored_probability − outcome)²
```

**Rationale.** Rewards accuracy over the full horizon and stops a lucky last-minute update
erasing months of bad forecasting.

**Exclusions.** Ambiguous resolutions are excluded from scoring entirely. Late-flagged
forecasts are *included* — they are penalised implicitly because the bad early probability
dominates the time-weighted average. The flag is informational.

---

## ADR 8 — Docker for local dev and deployment

**Status:** Accepted (v3)

**Decision.** `docker compose up` runs the full stack. SQLite in a named volume, no database
container.

**Amended (spec3.1).** One service, not two. The frontend became static files with no build
step (ADR 24), so it is bind-mounted into the `api` container and served by FastAPI rather than
built into an image of its own. The `api` service pins `--workers 1` — see ADR 25.

---

## ADR 9 — Daily batch updates, not event-driven

**Status:** Accepted (v3)

**Decision.** Once a day, re-run the agent on every active forecast. Write a new update only
when the probability moves by at least `MIN_PROBABILITY_DELTA` (3 points). Nothing else
triggers updates.

**Rationale.** P10 ("frequent, small updates") needs a disciplined cadence, not ad-hoc re-runs.
Daily catches meaningful developments and is cheap enough to run on everything open. The
threshold filters out the agent rephrasing the same view.

**Rules out (for now).** News-triggered or webhook updates, user-initiated re-runs, any other
schedule. `run_update_graph(id)` is callable from any trigger, so an event layer can be added
without restructuring.

---

## ADR 10 — A single agent in one structured call

**Status:** **Superseded by ADR 11** (v3 → v4)

**Decision was.** One Pydantic AI agent running decomposition, research, and synthesis in a
single structured call with `output_type=Forecast`.

**Rationale was.** The v2 implementation split these into separate prompts whose outputs were
then ignored. Forcing one structured call made the model commit to a complete, coherent
forecast in one pass.

**Why it was replaced.** The diagnosis was right for the problem of the time. But a single call
makes every principle a claim about a prompt rather than a property of an output — there was no
way to ask "does it find base rates" without running the whole pipeline and reading prose. The
new problem was measurability, and the single-call shape was the thing preventing it.

**Note.** This decision was already partly eroded before it was formally replaced: the shipped
v3 code had split into a two-phase research → synthesis pipeline inside `agent.py`.

---

## ADR 11 — One agent per methodology step

**Status:** Accepted (v4, spec3)

**Decision.** Eight `Agent` instances, one per methodology step, each with its own
`output_type` and system prompt. Five are orchestrated by a graph; three stand alone.

```
decompose      P1, P2         -> Decomposition
outside_view   P4, P7         -> OutsideView
inside_view    P5, 9, 14, 15  -> InsideView
synthesize     P6, P8, P16    -> Forecast
resolution     —              -> ResolutionCheckResult
update         P10, 11, 12    -> UpdateDecision
critic         P3             -> CriteriaCritique      (standalone)
postmortem     P13            -> PostMortem            (standalone)
```

**Rationale.** A step you cannot run in isolation is a step you cannot test. Each step now has a
named entry point (`run_decompose`, `run_outside_view`, …) a test can call with fixed inputs.
It also narrows each `output_type` enough that Pydantic validation does real methodology work:
`OutsideView.reference_classes` with `min_length=2` structurally guarantees the "multiple
lenses" half of P7 in a way no prompt can.

`critic` and `postmortem` sit outside both graphs deliberately — the criteria critic runs while
a question is being drafted, which is the only point at which fixing ambiguity is cheap.

**Supersedes.** ADR 10.

---

## ADR 12 — Pydantic graphs for orchestration

**Status:** Accepted (v4, spec3)

**Decision.** `pydantic_graph` for both the forecast pipeline and the daily update cycle.
Orchestration lives in `superforecaster/graphs/`; agents know nothing about each other.

**Rationale.** Both pipelines have real control flow — a retry cycle, a short-circuit, a
verification loop. Expressed as `if` statements those are invisible; as nodes they are
inspectable and individually testable.

The ordering guarantee matters most. P4 says "outside view first." As a prompt instruction that
is a hope. As the edge `FindBaseRates → AdjustInsideView` it is impossible to violate, because
the inside-view agent takes the base rate as an argument. The same applies to the daily cycle:
"resolution blocks the probability update" used to be a `flagged_ids` set passed between two
`for` loops, and is now an unreachable node.

`Graph.mermaid_code()` renders the real wiring, so diagrams in the specs cannot drift from code.

**Rules out.** Hand-rolled orchestration.

---

## ADR 13 — Methodology checks are pure functions

**Status:** Accepted (v4, spec3)

**Decision.** The checkable principles live in `superforecaster/checks.py` as pure functions
over Pydantic models returning `CheckViolation | None`. No LLM, no network, no I/O.

**Rationale.** This is the core of v4. A principle stated in a prompt cannot be tested; a
function over structured output can be unit tested in microseconds. `check_bayes_direction`
verifies the probability moved the same direction as the agent's own stated likelihood ratios —
arithmetic, which either holds or does not.

They are also a runtime feedback loop, not just a test fixture: the `Critique` node runs them
and routes failures back to synthesis with the specific violation attached, so a retry is a
correction rather than a re-roll.

**Rules out.** LLM-as-judge for these principles — slower, costs money, non-deterministic, and
no more correct than a comparison operator.

**Amended (2026-08-04) — a check may be advisory when its verdict is a judgment call rather
than arithmetic.** The original decision drew the line at "is it a pure function", which let a
heuristic and a piece of arithmetic sit in the same blocking retry loop. They do not behave the
same way under pressure.

`check_derivation` and `check_bayes_direction` are arithmetic: the numbers either reconcile or
they do not, and a retry that satisfies them has genuinely fixed something. The old
`check_calibration_hygiene` was a threshold on `Forecast.confidence` — a field the model wrote
itself — so a retry could satisfy it by *lowering* its own confidence label and retreating the
probability to the band edge. Nothing about the evidence changed. A gate that can be cleared by
relabelling is not enforcing a principle, it is teaching the model to relabel.

So a check may set `blocking=False`: it still runs, still reports, still travels out with the
result, but does not drive the retry loop. See ADR 29 for P16, the first one to use it.

This is a narrowing, not a reversal. ADR 13 stands unchanged for P1, P2, P6, P7, P9, P11, P12,
P14, and P15. The same change that made P16 advisory **added** three blocking checks —
`check_aggregation`, `check_citations`, and `check_linkage` — all of them set membership or
arithmetic. The checks layer gained enforceable ground while shedding a heuristic, which is the
distinction this ADR actually cares about.

---

## ADR 14 — Every threshold is configuration

**Status:** Accepted (v4, spec3)

**Decision.** Every tunable number in `checks.py` lives in `config.CheckThresholds`,
overridable by a `CHECK_*` environment variable. No numeric literals in `checks.py`.

**Rationale.** These thresholds are guesses until a backtest says otherwise. A hardcoded `0.20`
is a guess nobody can revise without editing code; a config value is a guess anyone can tune
from a scorecard.

---

## ADR 15 — No per-forecast granularity check

**Status:** Accepted (v4, spec3 — reversed during review)

**Decision.** P8 (granularity) is **not** enforced per forecast. There is no check that rejects
probabilities landing on a multiple of 0.05. It becomes a run-level statistic
(`round_number_rate`) instead, specified in spec4.

**Rationale.** An earlier draft failed any forecast that landed on an exact multiple of 0.05.
That is wrong: a forecast can legitimately be 0.60, and failing it punishes a correct answer.
P8 is about *rounding habits*, which is a property of a distribution, not of one number. On a
2-decimal grid, 21 of 101 values are multiples of 0.05, so an unbiased forecaster sits near
0.21 — a rate near 0.60 is the real signal.

**What replaced it per-forecast.** `check_derivation` (P6), which verifies the final
probability follows from the stated base rate plus stated adjustments. That catches the real
failure — abandoning a base rate for a narrative — without penalising round numbers.

---

## ADR 16 — A large move is verified, not capped

**Status:** Accepted (v4, spec3 — reversed during review)

**Decision.** A probability jump larger than `CHECK_LARGE_MOVE` (default 0.75) routes through
a `VerifyLargeMove` graph node instead of producing a violation. The node re-runs the update
agent in deep-verification mode: corroborate the decisive claim from a second independent
source, and search for evidence it is wrong or premature. It can only fire once.

**Rationale.** An earlier draft capped moves at 0.25 and failed anything larger. Genuinely
decisive events exist — FTX filing for bankruptcy, the FDIC seizing SVB — and a hard cap
rejects the correct behaviour. The right response to a big jump is to check whether it holds
up, not to forbid it.

`check_update_magnitude` keeps only its unambiguous half: under-reaction, where evidence
carries weight and the probability did not move at all.

---

## ADR 17 — Two clamps for contamination-free backtesting

**Status:** Accepted (v4, spec3)

**Decision.** Scoring against resolved questions clamps both the tools and the model.

| Clamp | Mechanism |
|---|---|
| 1 — tools | Tavily `end_date` + `topic="news"`, then `_drop_leaked` removes anything newer or undated. Wikipedia fetches the revision as of the date |
| 2 — model | `pick_clean_model(asked_at)` selects a model whose *training* cutoff predates the question |

**Rationale.** Contamination has two doors. Clamping only the tools leaves the model reciting
an outcome it memorised in training — it already knows how 2022 went. Clamping only the model
leaves it reading a 2024 article about a 2022 question. Both have to shut or the score is
fiction.

Every URL an agent sees is recorded on `ForecastDeps.sources_seen`, so a run can be audited for
leakage rather than trusted.

**Undated results are dropped**, which costs recall on purpose: an article with no publication
date cannot be shown to predate the question, and "probably fine" is not good enough.

---

## ADR 18 — `pick_clean_model` returns None rather than falling back

**Status:** Accepted (v4, spec3)

**Decision.** When no model has a training cutoff early enough for a question,
`pick_clean_model` returns `None` and the caller must skip. It never falls back to a newer
model.

**Rationale.** A skipped question is honest. A contaminated one is a number that looks real and
is not — and it would silently inflate the aggregate. Coverage being low is information; a
flattering score built on hindsight is worse than no score.

**Measured consequence (2026-08-03).** The earliest available training cutoff is Jul 2025
(Sonnet 4.5 / Haiku 4.5), so a question needs `asked_at >= 2025-10-29` to be clean-scorable.
The 66 legacy questions span Sep 2020 – Sep 2024 and give **0/66** coverage. This directly
caused ADR 21.

*(Opus 4.1 reached back to Mar 2025 but retired 2026-08-05 and was dropped from the garden
rather than let a backtest silently change behaviour mid-week.)*

---

## ADR 19 — Unit tests cover logic and contamination only

**Status:** Accepted (v4, spec3)

**Decision.** The `pytest` tier is scoped to pure logic and contamination guards. No test
asserts what a Pydantic field constraint already enforces at runtime.

**Rationale.** Pydantic validates shape, ranges, and `min_length` on every real run — a test
re-asserting `Field(ge=0, le=1)` is dead weight. What Pydantic cannot catch is a wrong sign in
a log-likelihood sum, or a date parameter silently missing from an API call. Both produce
plausible output and a wrong answer with nothing to flag them.

**Rules out.** A `test_agents_contract.py` asserting each agent declares its `output_type`.

**Honest limit.** A green `pytest` proves the plumbing is correct. It says nothing about
whether the prompts are any good — only the live tiers can.

---

## ADR 20 — Component golden data ships empty

**Status:** Accepted (v4, spec3)

**Decision.** The component test harness and all eight scorers ship. The eight
`evals/components/*.json` files ship as `[]`.

**Rationale.** A scorer encodes what "good output" means for an agent — the durable part, and
it is written. The cases are researched content: a base rate that is genuinely documented, a
planted fact that is genuinely irrelevant. Guessing at them produces cases that look like tests
and measure nothing.

Two scorers encode judgment worth recording. `score_resolution` treats a false positive as
fatal, because closing a live forecast is irreversible while missing a resolved one costs a
day. `score_postmortem` rewards calling a sound-but-missed forecast `sound_process` — a scorer
that penalised that would teach outcome bias, the exact failure P13 exists to prevent.

**Consequence.** Until the files are filled, P3 and P13 have no coverage at all — both are
standalone agents no graph exercises.

---

## ADR 21 — The end-to-end backtest is deferred

**Status:** Accepted (2026-08-03)

**Decision.** The backtest over resolved questions — `runner.py`, `scoring.py`, and the golden
question set — moves to `spec/planned/spec4.md` and is not built. Both clamps it depends on
ship.

**Rationale.** The corpus does not exist. The 66 legacy questions give 0/66 clean coverage
(ADR 18), so `test e2e --clean` would run zero questions and print an empty scorecard. Building
the harness against a corpus about to be replaced would waste the work; the harness shape does
not change, but the corpus determines whether clean mode is usable at all.

**Consequence.** There is currently **no measured accuracy number for this system.** That is
the single largest gap, and it should not be obscured by how much scaffolding exists around it.

---

## ADR 22 — Flat modules, except agents / graphs / evals

**Status:** Accepted (v3, amended v4)

**Decision.** `backend/` and `frontend/` split at the top level. Within the Python package,
modules are files rather than nested packages — with three exceptions: `agents/`, `graphs/`,
and `evals/`.

**Rationale.** The original rule was strictly flat. Eight agents as eight flat files would bury
the shared modules beside them, and the agents/graphs split *is* the architecture — orchestration
in one directory, agents in another, each ignorant of the other. `evals/` follows because it
carries data files alongside code.

**Related.** `ForecastDeps` lives in its own `deps.py` rather than in `graphs/state.py`, because
`tools` needs it and `graphs` imports `agents` imports `tools` — defining it beside the graph
state would be a circular import.

---

## ADR 23 — Spec lifecycle: planned → implemented

**Status:** Accepted (2026-08-03)

**Decision.** Specs live in two folders and move one way:

```
spec/planned/       being designed; not merged
spec/implemented/   shipped and merged to main
```

A spec is updated to match what was actually built, then moved to `implemented/`.
`CURRENT_STATE.md` describes what exists; `ADR.md` (this file) records why.

**Rationale.** A flat `change_specs/` folder gave no signal about which documents described
reality and which described intent. `SPEC_IN_PROGRESS.md` was the worst case — a name that
never stopped being true, describing work that had shipped.

**Consequence.** `SPEC_IN_PROGRESS.md` was deleted; `spec3.md` covers the same work and is the
version that matches the code. `TECHNICAL_DIRECTION.md` was deleted and its content became
this file.

---

## ADR 24 — A zero-build static frontend

**Status:** Accepted (spec3.1)

**Decision.** `frontend/` is five static files — `index.html`, `app.js`, `api.js`,
`admin.html`, `admin.css`. No `package.json`, no `node_modules`, no build step. FastAPI serves
them at `/` via `StaticFiles`, so there is one deployable instead of two.

**Supersedes.** ADR 4.

**Rationale.** The design that arrived (`Superforecaster.dc.html`) ships its own token system —
`--pv-*` variables, a light/dark toggle, a bespoke stream timeline. Layering that over MUI
means fighting emotion's specificity for every one of the fourteen event renderers, and ADR 4's
own rule was "no competing style system." The rationale that bought MUI — a consistent
component set without building a design system — is void once a design system exists.

Vanilla rather than React because the app is one page whose deepest tree is three levels. A
CDN React would add a network dependency and an import map to buy a diffing algorithm that a
full re-render at this size does not need.

**What is lost, explicitly.** Server-side rendering, and with it the indexability of public
forecast pages — ADR 4's other justification. Forecast pages are now client-rendered. If
indexability matters later, the fix is prerendering the `/forecasts/{id}` read view, not
reinstating Next.js for the streaming view.

**Consequence.** `docker-compose.yml` lost its `frontend` service; the `api` service
bind-mounts `./frontend` and sets `FRONTEND_DIR`. The six Next.js routes and eight components
were deleted; the four admin tabs were ported to `admin.html` as function rather than restyled
as product, because the new design does not cover them.

---

## ADR 25 — SSE for live runs, not WebSockets

**Status:** Accepted (spec3.1)

**Decision.** `GET /runs/{id}/stream` is `text/event-stream`, hand-rolled over a
`StreamingResponse`. No `sse-starlette`, no WebSocket.

**Rationale.** The data is one-directional — the only client-to-server message is "cancel",
which is a `DELETE`. SSE gets automatic reconnection with `Last-Event-ID` for free, survives
ordinary HTTP proxies, and needs no new dependency.

Every event carries a monotonic `seq` which is also the SSE `id:`, so a dropped connection
resumes at exactly the right frame. A fresh page load uses `?from_seq=` for the same thing. A
client cannot end up with a timeline that is missing its middle without being told: `replay()`
prepends a `truncated` event when the requested point has already been evicted from the ring
buffer.

**Rules out.** WebSockets, and long-polling. Also `X-Accel-Buffering: no` is set, because an
nginx in front will otherwise buffer the whole stream into one response and turn a live view
into a very slow page load.

**Known limit.** The registry is in-process, so this is `--workers 1`. Two workers would each
hold half the runs and a stream opened on the wrong one would replay nothing. Scaling out
means a shared bus behind `Run.emit` / `Run.subscribe`, which is why those are the seam.

---

## ADR 26 — Runs are memory-resident; the trail is kept client-side

**Status:** Accepted (spec3.1, amended)

**Decision.** A `Run` lives in an in-process registry with a ring buffer of its events. The
`runs` DB table stores only identity and terminal state — **no event rows on the server**. The
completed `Forecast` persists through the existing `save_forecast`. The reasoning trail is
persisted by the **browser**, one localStorage key per run, capped at `MAX_TRAILS` (12) with
oldest-first eviction.

**Amended (2026-08-04).** The original decision was that the trail was not persisted anywhere,
following the design's own line: *"The reasoning trail is not kept locally. Re-run this
question to watch it again."* That was wrong in practice — re-running costs five agent
invocations and a search budget to see something the user already watched once, and the run is
not reproducible anyway because the search results move. Reversed on request.

**Why client-side rather than a server events table.** The trail is a viewing artifact, not a
record: nothing downstream reads it, no score depends on it, and it is per-person. A server
table would need a schema, a retention policy, and a migration, to store something the browser
that watched it can hold for free. A run's trail measures ~20KB, so the 12-trail cap sits well
inside a 5MB origin budget.

**How a trail is recovered**, in order: this browser's localStorage; then
`GET /runs/{id}` while the run is still in the server's ring buffer (which also covers runs
started in another tab), and that snapshot is then written locally; then an honest "no stored
trail for this run."

**Consequence.** A restart still loses every *in-flight* run — the trail is only written when
the run ends. That is recorded rather than hidden: `init_db` flips orphaned `queued`/`running`
rows to `lost`, and the UI says the server restarted instead of spinning on a stream that will
never produce another frame. Clearing site data still loses finished trails, which is the
price of not owning a server-side table.

**Related.** `_finalize_if_orphaned` is a task done-callback covering the same failure one
level down — a task cancelled before the event loop gives it a slice never enters `execute`,
so its `finally` never runs and nothing would ever close the stream.

**Note (2026-08-05).** A saved trail now carries `columns` and `columnOrder` on each stage group.
Trails written by the pre-grid client have neither and take the existing path, so there is no
migration.

---

## ADR 27 — The UI projects typed state; it never asks for narration

**Status:** Accepted (spec3.1)

**Decision.** Every event the stream emits is derived from a field an agent already returns, or
from an existing `pydantic_ai` stream event. No prompt was changed to make the UI possible, and
no agent knows a UI exists.

| Event | Source |
|---|---|
| `sub` `note` | `Decomposition.sub_claims`, `.chain_note` |
| `ref` `analog` `note` | `OutsideView.reference_classes`, `.analogs`, `.disagreement` |
| `adj` `bias` `note` | `InsideView.adjustments`, `.bias_checks`, `.steel_man` |
| `draft` | `Forecast.probability`, `.confidence` |
| `check` `route` | `checks.run_forecast_checks_detailed` |
| `query` `source` | `FunctionToolCallEvent`, diffed `deps.sources_seen` |
| `thought` | `PartDeltaEvent` content deltas |

**Rationale.** The alternative — asking a model to narrate its own progress — produces a second
account that can disagree with the first. What the UI shows is then a story about the forecast
rather than the forecast. Projection makes divergence impossible: `build_waterfall` calls the
same `checks.signed_adjustment` that `check_derivation` uses, so the chart and the check cannot
tell different stories about what the evidence implies.

Two mechanisms carry it, both already present. `forecast_graph.iter()` yields nodes so state can
be read after each one. `ForecastDeps.emit` reaches the agents' own event stream handler through
`ctx.deps` — `run_agent` already forwards `deps` into `agent.run`, so no `run_<agent>` signature
changed.

**Extended (2026-08-04) — a verdict you can argue with.** Two additions, both projections of
data that already existed:

- **`check.evidence`** carries the material each check reasoned over, pass or fail: the
  anchor-plus-adjustments walk for P6, the class rates and spread against the threshold for P7,
  the five bias slots and which were filled for P15. Built by `checks.check_evidence`, a pure
  function beside the validators rather than inside them — they answer pass or fail, and this
  answers "on what basis". A violation's `detail` states a conclusion; this is what you check
  that conclusion against.
- **`brief`** is emitted when Synthesize starts a second attempt, carrying the *literal* text
  the retry prompt contains, built from the same `_violation_block` and `_arithmetic_block`
  that `run_synthesize` uses. A retry that looks like a re-roll from outside is one nobody can
  audit; showing the correction verbatim is what makes the two attempts distinguishable. The
  formatters are now shared rather than duplicated, so the displayed text cannot drift from the
  sent text.

**Honest limits, carried in the UI rather than papered over.** `thought` events only appear when
the model emits thinking or text before its structured output, so a stage can legitimately show
tool calls and no narration. `source.credibility` is `null` because nothing scores a domain.
`check` events carry `detail` only on failure, because the seven validators return
`CheckViolation | None` and a pass has no message — the UI shows the check name alone rather
than inventing one, and `evidence` now fills that gap with numbers instead of prose.

**Extended 2026-08-05 — the grid.** Every event gains a `sub_claim` tag on the envelope, and two
new types appear.

- **`column`** opens one card per sub-question at the *top* of a fanned-out row, before any agent
  starts. Every field is read off state an agent already produced: the sub-questions from
  `Decomposition`, and on the inside row the incoming rate and classes from the `OutsideView` the
  previous node wrote. `researching` is derived from `knowability`, not asserted — a `judgment`
  column gets a card that says there is nothing to look up, rather than vanishing from the row.
  This is why `GraphHooks.stage_started` widened to carry the state: without it the hook has no
  decomposition, and a row of four concurrent searches stays blank for minutes.
- **`exhausted`** reports a cell that crossed its hard budget. Pure `SearchBudget` state — code,
  not a model.

`sub_claim` sits on the envelope beside `stage` and `attempt` rather than inside `payload`,
because it is a coordinate rather than content: the client routes on it before it reads the
payload, and `observability` — which builds three different payload shapes and has no idea the
grid exists — would otherwise have to inject it into all three. It forwards an opaque tag.

The rule holds: no prompt was changed to make either event possible.


---

## ADR 28 — A failed run resumes from its last completed node

**Status:** Accepted (2026-08-04)

**Decision.** Every run is checkpointed with `pydantic_graph`'s `FileStatePersistence`,
one JSON file per run under `RUN_CHECKPOINT_DIR`. On failure the checkpoint is kept and
`POST /runs/{id}/resume` re-runs **only the node that died**. On success it is deleted.

**Rationale.** A forecast run is five agent invocations with a live search budget, and the
failure that prompted this — `UsageLimitExceeded` on the inside view — happens *after* the
decomposition and the base rates have already been paid for. Throwing that away and starting
over is the expensive kind of failure, and it is the common one: a usage limit fires mid-node,
not at the start.

A graph node is exactly one agent call, so pydantic-graph's own snapshot granularity is the
granularity that matters. No new persistence machinery was needed — only the recovery step
below.

**The wrinkle this exists to handle.** `FileStatePersistence` marks a snapshot `'error'` when
its node raises, and `load_next` returns only snapshots with status `'created'`. A failed run
is therefore *not* resumable as shipped: `iter_from_persistence` finds nothing and raises.
`checkpoints.rewind_for_resume` flips the stalled snapshot back to `'created'` — that one
function is the whole difference between a checkpoint and a post-mortem. It also catches
`'pending'` and `'running'`, which is what a process death leaves behind: no exception, but
equally unfinished.

**Resume raises the budget by default.** The usual reason to be here is that the old budget
ran out, so resuming with the same one walks into the same wall. `ResumeRunRequest.max_iterations`
overrides it, the error event carries a `hint` saying so, and the UI prefills double the
previous depth.

**The event stream continues rather than restarting.** `seq` keeps counting across the resume,
so a client reconnecting with `?from_seq=` sees more of the same run instead of a second run
that happens to share an id. A `resume` event marks the seam and names the node being re-run.

**Rules out.** Auto-retry on failure. A run that failed for a reason resuming will not fix
would loop, and the failure is usually a budget or a provider problem that wants a human
decision. Resume is a button.

**Related.** The per-iteration research budget became configuration in the same change
(`RESEARCH_TOOL_CALLS_PER_ITERATION`, `RESEARCH_REQUESTS_PER_ITERATION`, both defaulting to 3
where the literal was 2). At the old rate the default `max_iterations=5` capped a researching
agent at ten tool calls, which a normal run reaches — per ADR 14, a number that tight has no
business being a literal.

---

## ADR 29 — An extreme probability is justified, not forbidden

**Status:** Accepted (2026-08-04)

**Decision.** P16 (calibration hygiene) no longer blocks. `check_calibration_hygiene` sets
`blocking=False` and keys off a new `Forecast.extreme_justification` field: a probability
outside `[calibration_floor, calibration_ceiling]` is allowed, but the agent has to write down
which reference class carries the extreme, why the spread between classes does not undercut it,
and what would have to be true for it to be wrong. The check flags the extremes nobody argued
for, and the ones hugging the band edge while the reference classes disagree.

**Rationale.** A live run produced `probability 0.005 outside [0.02, 0.98] with
confidence='medium' and a reference-class spread of 0.30`. Attempt 2 "fixed" it by moving to
exactly `0.02` and *lowering* confidence to `low`, leaving the spread at 0.30. It passed,
because the band test was `floor <= p <= ceiling` and landing on the boundary skipped the
earned-extreme arm entirely.

Two things were wrong, and only one was the off-by-one.

The gate read `Forecast.confidence`, which the model wrote itself. The synthesis agent has
`tools=[]` and receives the frozen `OutsideView` on retry, so it *cannot* earn an extreme with
new evidence — the only moves available are relabelling and retreating. A gate whose two exits
are both cosmetic does not enforce calibration; it selects for cosmetics.

And a hard cap on boldness fails correct answers, exactly as ADR 15 found for P8 and ADR 16 for
large moves. Some questions really do resolve at 0.5%. The right response to a bold number is
to make the agent argue for it, not to forbid it — ADR 16's sentence, applied to a probability
instead of a jump.

**What replaced the confidence field.** Nothing, at the forecast level: it was deleted. It was
self-reported, undefined in `spec/superforecasting_methodology.md`, and read by exactly one
consumer — the gate this ADR removes. Confidence is now a property of the edge between a source
and a claim (`GradedSource.confidence`), aggregated to a forecast-level figure that is derived
rather than asserted. The model has no field in which to state its own confidence, which is the
point.

**What the checks layer gained in the same change.** `check_aggregation` (the stated
`aggregate_base_rate` must match the weighted mean its own reference-class weights imply),
`check_citations` (every cited URL must appear in `deps.sources_seen`), and `check_linkage`
(sub-claim references must resolve). All three block, and all three are arithmetic or set
membership. See the ADR 13 amendment.

**Rules out.** Reading a self-reported confidence label in any check. If a field exists only so
a validator can threshold it, the model will learn to write whatever clears the threshold.

---

## ADR 30 — Research fans out per sub-question, inside the node

**Status:** Accepted (2026-08-05)

**Decision.** Decompose fixes a grid: rows are stages, columns are sub-questions. Both research
rows — `FindBaseRates` and `AdjustInsideView` — run one agent per column concurrently and merge
at a barrier. The fan-out lives *inside* `run_outside_view` / `run_inside_view`. No graph node
was added; `forecast_graph.get_nodes()` still equals `STAGE_KEYS`.

**Rationale.** One agent with fifteen tool calls covering three to five sub-questions spends
them on the most searchable one. That is not a prompt failure — nothing in the prompt was ever
going to allocate a shared budget fairly across parts of a question the agent cannot see the
difficulty of in advance. Per-column agents make the allocation structural, and the whole row
now costs roughly a quarter of the wall-clock.

**Why the seam is `run_<agent>` and not the node.** ADR 11 names `run_<agent>` as the one seam a
test, an eval, and a graph node all call; nine sites in `test_checkpoints.py` monkeypatch exactly
those two functions. And ADR 12 scopes `graphs/` to methodology sequencing — a parallel map of
one agent over N inputs is a row's internal shape, not an edge. Keeping the fan-out below the
seam is what leaves the `FindBaseRates → AdjustInsideView` edge, and P4's ordering guarantee,
untouched.

**Amends ADR 12** rather than superseding it. Two assertions are the canaries: node names equal
`STAGE_KEYS` (no node was added), and every stage starts before it finishes (the fan-out stayed
inside the node, so the barriers are still the graph's). If either goes red, the fan-out has
escaped into `graphs/`.

**What became structurally impossible.** `_merge_base_rates` stamps `sub_claim_ids` on every
reference class *unconditionally* — a cell researched exactly one column, so letting the model
volunteer a different id would re-open the linkage hole `check_linkage` closes. The group of
classes belonging to no column at all, which the old flat prompt produced routinely, no longer
has a way to exist. `group_by_sub_claim`'s trailing "the question as a whole" group is deleted.
Same move as ADR 12 made for ordering: convert a checked property into an unrepresentable state.

**A failed column degrades; a failed row does not.** `asyncio.gather(return_exceptions=True)`, so
one cell throwing cannot cancel its siblings mid-search. A column that returns nothing contributes
no classes and falls back to its own working estimate in `checks.chain_inputs`. Only when *every*
column fails does the row raise and hand over to ADR 28's checkpoint.

**Rules out.** A `sub_claim_ids` instruction in any research prompt — the link is code's to
assert. And sharing one `sources_seen` list across concurrent cells: `observability` detects new
sources by slicing the tail off that list, so a shared list hands each cell the other's sources.
Each cell gets a private list, merged after the barrier.

---

## ADR 31 — P14 and P15 move to a reflect pass

**Status:** Accepted (2026-08-05)

**Decision.** The inside-view row produces per-column adjustments plus a per-column steel-man.
The whole-question `steel_man`, `what_would_change_my_mind`, and all five `bias_checks` come from
a new no-tools `reflect` agent that runs after the row's barrier with every column's adjustments
in front of it.

**Amends ADR 11's table:** `inside_view → P5, P9` and `reflect → P14, P15`.

**Rationale.** Four columns produce twenty bias checks and `InsideView` wants exactly five, on a
closed set of names. There is no honest merge: concatenating four `confirmation` assessments
produces text no reader wants, and picking one discards three. Either way the artifact stops
meaning what `check_bias_coverage` reads it as.

Worse, three of the five are questions about the *final probability*. "Would I give the same
number for a 10x bigger version" and "am I stuck near the first number I saw" have no referent
inside a column, which has no number. Asking anyway produces five plausible paragraphs about
nothing — which is precisely the failure P15 exists to catch.

And `check_disconfirming` fails when every adjustment points the same direction. No column can
evaluate that; it cannot see the others' directions. The reflect pass can, so the instruction and
the check are finally about the same set.

The `INSTRUCTIONS` were lifted from `inside_view` verbatim rather than rewritten, so the wording
that produced today's outputs is preserved. Cost is one request against `get_synthesis_limits()`
— no tools, no search budget.

This is arguably *more* faithful to ADR 11's own rationale ("a step you cannot run in isolation
is a step you cannot test"): `run_reflect` is testable against a fixed adjustment list with no
network at all.

---

## ADR 32 — The search budget is a gradient, and it is per cell

**Status:** Accepted (2026-08-05)

**Decision.** Two thresholds per cell instead of one wall for the row. `soft_depth` — the cline —
is where the agent starts being pushed to stop searching and commit. `hard_depth` is
`UsageLimits.tool_calls_limit`. A cell that crosses the wall degrades to no result; the row and
the run continue. Configured by `CELL_SOFT_CALLS_PER_ITERATION` and `CELL_HARD_HEADROOM`.

**Rationale.** A wall with no warning is the worst possible shape: the agent searches at full
tilt and then dies mid-thought, and `UsageLimitExceeded` killed the whole graph. ADR 28 made the
budget configurable *because* hitting it killed a run — that motivation is now addressed at the
source rather than worked around at the resume button.

**Two channels, and why both.** The primary one is a notice appended to every tool return. It
arrives at the exact moment the decision is made — the model just asked for a result and is about
to read it, so there is no attention to compete for and nothing to skim past. It is also the only
channel that can say "this is your last tool result."

Its two blind spots are real: there is no tool result before the first request, and after the
last one it goes silent while the model may still make several more requests (an output-validation
retry, a text-only turn). A dynamic `@agent.instructions` function covers both. Instructions are
re-fetched per model request, which is what makes the pressure escalate *within* a run — unlike
the static `SEARCH BUDGET: at most N rounds` line it replaces, which was frozen into request 1.

**The counter lives on `ForecastDeps`, not a contextvar.** `agent.override` and
`capture_run_messages` already use contextvars; a third whose correctness rests on "gather wraps
coroutines in Tasks, and Tasks copy the context" is three implicit couplings where a dataclass
field is zero.

**`used` is incremented by the tools**, not read off `RunContext.usage` — a tool needs the count
at return time and `usage.tool_calls` increments afterwards. And `find_disconfirming_evidence`
runs three searches inside one tool call, so it increments once: `SearchBudget.used` and
`UsageLimits.tool_calls_limit` have to count the same thing, or the cline fires at a depth the
wall does not agree with.

**The one case degradation cannot absorb.** If *every* column exhausts, there is no outside view
to build — `OutsideView` requires two reference classes and there are none. That raises, with the
per-column depths spent in the message, and ADR 28's resume-with-a-higher-depth is the answer.

**Rules out.** Auto-raising the budget on exhaustion. The depth is the operator's call, same as
ADR 28's "resume is a button."

---

## ADR 33 — The anchor is the chain the decomposition describes

**Status:** Accepted (2026-08-05)

**Decision.** `Decomposition` gains a typed `chain_rule` (`conjunction` | `disjunction` |
`custom`). `aggregate_base_rate` is that rule applied to the per-column rates — the product for a
conjunction, `1 - prod(1 - p)` for a disjunction — computed at merge by `checks.anchor_from`.
`custom` falls back to the weighted mean across all classes, which is what the anchor was before.

**Rationale.** The anchor was a weight-weighted *mean* across every reference class, regardless
of which sub-question each one answered. For a conjunctive question that is not imprecise, it is
the wrong operation, and it is biased in a known direction: the mean of factors each below 1 is
always greater than their product.

    sc1 0.55 · sc2 0.70 · sc3 0.60 · sc4 0.80
    mean    0.66     <- the old anchor. Answers no question anyone asked.
    product 0.185    <- what the decomposition actually says.

Every conjunctive question was getting an inflated anchor by construction, and the entire P6
chain hangs off it. `chain_note` has always asked the decompose agent for this distinction —
"multiply for a conjunction, take the maximum for alternatives, and say which it is" — and
nothing could read the answer.

**The empty-cell trap.** A product over only the *researched* columns silently treats the rest as
1.0. `checks.chain_inputs` runs over every sub-question, falling back to `SubPrediction.probability`
— the decompose agent's own working estimate, an existing typed field — and marks the row
`estimated` so a reader can tell the two apart.

**Amends ADR 29, and weakens what it added.** `check_aggregation` was the check that made a
model-asserted anchor accountable to its own weights. Now the merge computes the anchor with
`anchor_from` and the check re-derives it with `anchor_from`, so no model performs that arithmetic
and the check cannot catch one performing it badly. It has become a guard on the *artifact*:
drift between the merge and the rule, a hand-built fixture, a checkpoint resumed from an older
version.

That is a real loss, traded for making the failure structurally impossible rather than merely
checked — the same move ADR 12 made for "outside view first" and ADR 30 for sub-claim linkage.
It is written down here, and in the function's own docstring, so it is not later discovered as a
tautology nobody chose.

**Rejected:** adding a `rate` field to each cell's output and checking it against
`sub_claim_rate`. That re-introduces a model-asserted number one function call away from the
weights that imply it, for a check that fires only when the model's mental arithmetic is wrong
about two or three numbers it can see.

**P7's spread is now measured within a column.** `base_rate_spread` is max-minus-min across all
classes, which meant something when every class measured the same thing. Once each measures a
different sub-question it measures nothing: a 0.15 lens on "will they commit" and a 0.80 lens on
"will they pick an exchange" are not disagreeing, and the old number called that 0.65.
`check_dragonfly` now fires on the widest *column* and names it; `check_calibration_hygiene`'s
second arm uses the same, because post-fan-out the global spread is wide by construction and the
advisory would otherwise fire on every run.

**Known approximation, deliberately left.** `check_derivation` and `build_waterfall` still add
signed adjustments to `aggregate_base_rate` flat, while an adjustment is now a delta from *its
column's* rate. Changing that means propagating each column's adjusted rate back through the
chain rule, which is self-contained work with its own failure modes, and `check_derivation` is
the load-bearing P6 check with two test suites pinning it. The cell prompt compensates —
magnitudes are points on the final probability arrived at via this sub-question, which is what
the old global-anchor prompt already assumed. Filed as the follow-on.

**ADR 29 is not reversed.** The grid asks for a per-base-rate confidence, and it already exists
derived: `checks.claim_support` grades a reference class by its *strongest* `GradedSource`. No
self-reported confidence field returns.

---

## ADR 34 — Schema migrations are a version counter and a dict

**Status:** Accepted (2026-08-05)

**Decision.** `db.init_db()` runs `_migrate` after its `CREATE TABLE IF NOT EXISTS` block.
`MIGRATIONS` maps a version number to the statements that take the schema from `version - 1` to
`version`; `PRAGMA user_version` — four bytes SQLite already keeps in the file header — records
where a given file is. No Alembic, no migrations directory, no dependency.

**Rationale.** The gap this closes was not theoretical, and its shape is the argument. ADR 29
deleted `forecast_updates.confidence`: the INSERT stopped supplying it and the fresh-schema DDL
stopped declaring it. Both changes were correct. But `CREATE TABLE IF NOT EXISTS` does exactly
nothing to a table that already exists, so every deployed database kept a `NOT NULL` column that
nothing wrote.

The failure mode is what makes this worth an ADR. Every run completed all five stages, spent its
full search budget, produced a real forecast — and then died on the final write with
`IntegrityError`, rendered under Critique because that was the last stage to draw. The offered
remedy was "resume the failed step", which could not work: no step had failed. A schema drift
that only surfaces after a complete agent run is the most expensive kind of drift there is.

**Why not Alembic.** It was the agreed fix for a year and never got built, which is evidence
about its size relative to this problem. The whole mechanism here is a counter and a dict, it has
no dependency, and it survives copying the file. If the schema ever needs branching or downgrade
paths, Alembic is still the answer — this does not preclude it.

**Every step must tolerate having nothing to do.** A database created fresh by `init_db` is
already at the current schema but its `user_version` is 0, so every step still runs against it.
`_migrate` swallows `no such column` for exactly this reason and re-raises everything else. That
asymmetry is deliberate: a step that finds its work already done is normal, and a step that fails
for any other reason must not be silently marked complete.

**Rules out.** Telling an operator to delete the database. The data in it is forecasts scored
against resolved questions — the only record of whether this system works.

---

## ADR 35 — An unset admin key means "not deployed", not "misconfigured"

**Status:** Accepted (2026-08-05)

**Decision.** With `ADMIN_API_KEY` unset, admin routes accept unauthenticated requests that
arrive from a loopback address and carry no proxy header. Everything else still 403s or 500s
exactly as before. The frontend asks the server which case it is via a new public
`GET /config`, rather than deciding for itself.

**Rationale.** The setup was two keys and one command, and then the first click failed. You
exported an API key, started the server, typed a question, pressed **Run now**, and got
"Admin token not set" — from `api.js`, which refused before sending anything. The remedy was
to invent a value, put it in a `.env` you did not otherwise need, restart, then paste the
same value into a `window.prompt` behind a button labelled Admin. Four steps of ceremony
around a secret whose only purpose was to authenticate you to yourself.

On a laptop, where the only thing that can reach the port is the process's own owner, a
token protects nothing. It is worth something the moment the port is reachable by anyone
else — so the condition is not "is a key set" but "could this request have come from
somewhere else."

**Loopback plus no proxy header, and why both.** `request.client.host` in `{127.0.0.1, ::1}`
is the local case. But anything upstream can rewrite that, and a reverse proxy in front of
this is precisely the shape of a real deployment — so any of `X-Forwarded-For`,
`X-Real-IP`, `X-Forwarded-Host` or `Forwarded` disqualifies the request regardless of what
the socket says. Two independent signals, and the failure mode of each is to demand the key.

`superforecaster serve` binds `127.0.0.1` by default, which keeps that statement true by
construction rather than by hope.

**The client stopped deciding.** `api.js` refusing on a missing token was a second, stale
copy of the server's auth policy. `GET /config` returns `auth_required` and the client
believes it; the Admin button is hidden entirely when there is nothing to authenticate. A
server is the only thing that can answer a question about its own auth.

**Rules out.** Defaulting `ADMIN_API_KEY` to a known value, and generating one on first
boot. Both produce a system that looks authenticated and is not, which is worse than one
that says plainly it is running unauthenticated on localhost — as the startup banner now
does, every time.

**Related.** The same banner reports whether `TAVILY_API_KEY` is set. A forecast built on
Wikipedia alone is shaped exactly like one with web search behind it — same checks, same
confidence, thinner reference classes — so the difference has to be stated rather than
inferred. It also appears as a header chip in the UI.

---

## ADR 36 — The fan-out is a graph edge, not an `asyncio.gather`

**Supersedes ADR 30**, which put the per-column fan-out *inside* `run_outside_view` /
`run_inside_view` on the reasoning that "a parallel map of one agent over N inputs is a
row's internal shape, not an edge."

The reasoning held right up until you had to read it. What ADR 30 bought was a stable
`run_<agent>` seam; what it cost was orchestration written by hand in a leaf function —
`asyncio.gather(..., return_exceptions=True)`, a `zip(cells, results)` to re-pair inputs
with outputs, `isinstance(r, BaseException)` filtering, and a manual re-raise of the first
exception. Four mechanisms to express "run these, wait, keep the ones that worked."

`pydantic_graph.beta`'s `GraphBuilder` expresses that as one edge:

```python
g.edge_from(decompose).map().to(base_rate_cell)
g.edge_from(base_rate_cell).to(collect_base_rates)
```

**What this changes.** `forecast_graph` is now a `GraphBuilder` graph of six steps with two
`map`/join pairs and a decision-node retry cycle. `run_outside_view` and `run_inside_view`
are gone; the modules supply `run_base_rate_cell` / `run_inside_view_cell` and the merge.
`asyncio.gather` no longer appears in production code at all.

**Isolation is now structural.** A cell returns a `Cell` carrying its own `sources` list,
which the merge step folds in after the join. ADR 30 achieved the same thing by convention
— a private `sources_seen` that the row remembered to merge. The docs for the beta API warn
that "all parallel tasks share the same graph state", so the convention would have been the
only thing standing between two columns and each other's citations.

**Rules out.** Keeping `gather` behind a `fan_out()` helper. It removes the duplication and
none of the hand-rolled concurrency, which was the actual complaint.

**Known cost — this is a beta API.** It lives at `pydantic_graph.beta` and its import path
will move. The exposure is one module. Two limitations found and worked around:

- **An empty `.map()` stalls the runner** — "Graph run completed, but no result was
  produced." Each research row therefore routes through a decision, so a row with no
  columns bypasses the fork entirely (`no_base_rate_cells` / `no_inside_cells`). Worth
  reporting upstream.
- **Decision branches match by `isinstance`**, so a parameterized generic like
  `list[SubPrediction]` is not a valid branch source. The branches match bare `list`.

**Related.** `reflect` becomes its own step in the same change. It was a tail call at the
end of `run_inside_view` — an agent run hidden inside a leaf function, invisible to the UI,
to the trace, and to the checkpoint. Every agent run is a node now.

---

## ADR 37 — Durability is DBOS, not a checkpoint file

**Supersedes ADR 28**, which resumed a failed run from `pydantic_graph`'s
`FileStatePersistence`.

ADR 28 worked, but `checkpoints.py` existed almost entirely to defeat the library it wrapped:
`FileStatePersistence` marks a snapshot `'error'` when its node raises, and `load_next` only
returns snapshots with status `'created'`. A failed run was therefore not resumable as-is,
and `rewind_for_resume` flipped the status back by editing the JSON. A checkpoint you have to
repair before using is a post-mortem.

Independently, ADR 36's move to `GraphBuilder` settles it: the beta graph builder has no state
persistence at all, and says why — "the complexity of achieving consistent snapshotting with
parallel execution." Snapshot-the-whole-state was never going to survive a fan-out.

**DBOS instead.** It runs fully in-process as a library — no server — and checkpoints to
SQLite. Every agent call inside the workflow is a step, so resuming re-runs only the step that
failed. `checkpoints.py` is deleted; `durability.py` is 71 lines of configuration.

**Verified, including the case that would have killed it.** A parallel `GraphBuilder` graph
inside a `@DBOS.workflow()` resumes correctly, memoizing durably-completed steps — and does
**not** cross-wire results when the replay's completion order differs from the original run's.
That was the real risk: DBOS documents that parallel tool execution "cannot guarantee
deterministic ordering", and agent latencies vary, so replay order will differ in production.

**Resume is in-process, deliberately.** `executor_id` is unique per process, so a restart does
not adopt the previous process's pending workflows. `RunRegistry` is in-memory (ADR 26), so a
recovered run would have no event stream and no subscribers — nothing to report to. `db.init_db`
marks those rows `lost`, which is the honest answer. Cross-restart recovery means giving the
workflow serializable arguments and rebuilding the run from its database row; it is a change to
`durability.py` and `runs.execute`, and nothing else.

**Durability is a capability, not a requirement.** `durability.configure()` is called from the
API lifespan only. A one-shot CLI forecast, an eval, and a test have nothing to resume into and
run the same graph one layer thinner. `@DBOS.workflow()` refuses to be *called* before DBOS
initializes, so `_run_forecast` (plain) and `_forecast_workflow` (wrapped) are separate objects
rather than one function checking a flag.

**Known cost.** DBOS depends on `sqlalchemy` and `psycopg`, and is Postgres-first — SQLite is
a development-oriented extra. We run SQLite anyway: single worker, two database files, no
migration of live data. Revisit at multiple workers, where Postgres and SQLAlchemy Core become
the natural move regardless, since the raw SQL in `db.py` is SQLite-dialect.

---

## ADR 38 — The backend streams typed objects; the frontend decides what to draw

**Amends ADR 27**, which said the UI projects typed state and never asks for narration. That
principle stands. What changed is where the projecting happens.

`runs.py` had ~340 lines of `project_*` functions turning `Decomposition`, `OutsideView`, and
`InsideView` into small display dicts — renaming `magnitude` to `mag`, `base_rate` to `rate`,
grouping reference classes under sub-claims, folding three models into a waterfall. That is
layout encoded in Python, 600 lines from the CSS it serves, and every rename was a place the
UI and the methodology could drift apart silently.

**Now:** each step emits the object its agent returned, dumped. `decompose`, `outside`,
`inside`, `synth` carry a whole `model_dump()`. The frontend groups, renames, and charts.

**What did not change.** Live progress — `query`, `source`, `thought` — still comes from
pydantic-ai's own `event_stream_handler` in `observability.py`. Those are inherently
incremental and have no whole-model equivalent. Stage boundaries are ordinary events too now
(`stage` / `stage_end`), which is what let the `GraphHooks` protocol and the node-by-node
driver in `run_forecast_graph` be deleted: one mechanism instead of two.

**Cut outright.** The waterfall (`build_waterfall`) and the per-check evidence payload. The
first is a chart the frontend can compute; the second becomes a static principles drawer with
hover-highlight, which is a frontend feature and needs no backend logic.

**Cost, stated plainly.** Some of what was deleted was not reshaping but *cross-model
computation* — the waterfall folds outside + inside + forecast; `_class_payload` joined cited
URLs against `sources_seen` to answer "which search found this base rate". That work does not
vanish, it relocates to a frontend with no build step, no types, and no tests, where a field
rename fails silently as blank DOM. Accepted deliberately: the backend should not own layout.

**Related.** `api/runs.py` now uses `sse-starlette` for framing, keep-alive pings, and
disconnect detection, replacing a hand-rolled `asyncio.wait_for` loop. The buffer, replay, and
subscriber fan-out moved to a generic `eventstream.py` that knows nothing about forecasting.

**Correction (the first implementation of this was broken).** ADR 37 shipped with the run
wrapped in a workflow and *nothing inside it registered as a step*. The docstrings claimed
"every agent call is a DBOS step"; the only matches for that phrase in the codebase were
the docstrings. Two consequences, both silent:

- A workflow with no steps has nothing to replay, so a resume re-ran the entire graph.
- `DBOS.resume_workflow` on a workflow that reached a terminal error replays the recorded
  exception and executes nothing at all, so a failed run could never recover — strictly
  worse than the `checkpoints.py` it replaced.

No test caught it because `durability.configure()` is called from the API lifespan only,
so the whole suite ran with `is_active() == False` and exercised the un-checkpointed
branch. **The production path had zero coverage.** That is the real defect; the rest
followed from it.

The fix rests on two non-obvious facts, both verified rather than assumed:

- **DBOS steps do not serialize their arguments, only their return values.** That is what
  lets `durability.agent_step` wrap an agent call whose arguments include a live `emit`
  callable and a model client. The return value is a Pydantic model, which serializes.
- **`fork_workflow(id, start_step)` takes a `function_id`, not a list index**, and keeps
  every checkpoint with `function_id < start_step`. Ids are 1-based; reading the id off
  the failed step's record is correct, counting list positions is off by one.

Forking mints a new workflow id, so `Run.workflow_id` tracks the current one — a second
failure forks from the fork.

`tests/test_durability.py` now covers the durable path, counting real agent invocations,
because a workflow that replays a recorded result and one that re-executes are
indistinguishable from the outside. It holds one event loop for the module and calls
`durability.shutdown()` on teardown: DBOS is process-global and binds its thread pool to
the loop that launched it, so leaving it running makes every later test take the durable
branch against a closed loop.

---

## ADR 39 — A base rate is counted, not stated

**Supersedes the base-rate half of ADR 30.**

`ReferenceClass.base_rate` was a float the model asserted. `sample_size` was a float the
model asserted. `analogs` was a list the model volunteered. **None of the three was read
by any code** — grep found `sample_size` and `analogs` only in the model definition, one
prompt string, and the UI. A class could claim `base_rate: 0.85, sample_size: 50` while
listing two analogs, one of which said no, and nothing anywhere would notice.

A `ResearchedLens` now carries `evidence`, and the rate is `Σ hits / Σ n` — a property,
not a field. Two kinds of block, audited differently:

- **counted** — cases the agent enumerated. `check_base_rate_derivation` matches `n`
  against the analogs listed and `hits` against how many resolved yes. This is what makes
  "7 of the 10 cases I found did, so 70%" a fact rather than a claim.
- **published** — a statistic somebody else measured, which is the only way an `n` of 230
  enters a forecast nobody could enumerate by hand. Audited by provenance: a source is
  required, and `check_citations` verifies the URL was retrieved.

They pool into one denominator: `7/10` counted plus `140/230` published is `147/240`. A
handful of verified cases and a large published study sit in the same rate, each carrying
exactly the weight of its own denominator.

**Rules out.** Counted-only, which would ban real datasets and push the agent to
enumerate instead of finding the best measurement. And model-stated with analogs as
decoration, which is what this replaces.

---

## ADR 40 — Modifiers move a lens, and lenses are chosen before they are measured

**Supersedes the inside-view half of ADR 30 and the arithmetic in ADR 36.**

Two defects, found by reading a real run's output rather than its code.

**The prompt said two incompatible things.** `inside_view` told each cell "every
adjustment is a signed delta from {sub-claim rate}" and, nine lines earlier, "magnitudes
are points on the FINAL probability". Meanwhile `implied_probability` summed every
adjustment flat onto the whole-question anchor, ignoring `chain_rule` entirely. The model
was being asked to mentally rescale a sub-claim delta into final-probability points, and
nothing verified it had.

**A modifier is only meaningful relative to a population.** "The market cap exploded" is
already inside *large-cap tech IPOs* and warrants no move; against *all AI labs* it is the
entire differentiator. Same fact, opposite treatment, decided by which population you are
looking through. Adjusting a *blended* rate double-counts against the populations that
already control for the feature and under-counts against those that do not — so the
adjustment has to happen per lens, before the blend.

The pipeline is now:

```
lens rate = Σ hits / Σ n                        derived
adjusted  = lens rate + its own modifiers       per population
sub-question = Σ(weight × adjusted) / Σ weight  relevance-weighted
final = chain_rule over sub-questions           the same combine_sub_claim_rates
                                                the anchor uses
```

`check_derivation` is load-bearing again. It previously compared a flat sum against a
number that same flat sum had been used to suggest.

**Choosing lenses is its own step, with no tools and no rates.** An agent that chose
populations and measured them in one pass could settle on whichever gave the answer it
already liked, and the output would be indistinguishable from an honest one. Naming them
blind is pre-registration. It also makes the lens the unit of parallelism: three lenses
across five sub-questions is fifteen concurrent searches rather than five.

**The blend ignores `n`, deliberately.** A lens measured over 12 cases can and should
outweigh one measured over 230 when it fits better — sample size measures how well a
population was *measured*, not how much it *resembles this case*, and only the second is
what a reference class is for. Precision-weighting was considered and rejected for exactly
this reason: it would let a large, well-measured, ill-fitting population dominate.

`weight` is therefore **the only number left in the pipeline that no check can verify**.
Everything else is derived from evidence and re-derivable. So `weight_rationale` is
mandatory, and the UI shows every lens's own adjusted rate — a reader can see what each
population alone implied and judge the blend rather than take it.

**Related.** `check_evidence` (144 lines) and `run_forecast_checks_detailed` are deleted;
the three hand-synchronised tables they belonged to collapse into one `FORECAST_CHECKS`
registry of `(name, principle, label, run)`. `synthesize._implied` — a hand-copied second
implementation of the derivation formula, and precisely how the prompt and the check
drifted apart — is deleted in favour of calling `checks.implied_probability`.

---

## ADR 41 — Scoring is arithmetic, not persistence; and no ORM

`db.py` was 1138 lines, and the audit's first instinct — reach for SQLAlchemy — turned out
to be wrong when measured against the actual file:

| candidate | LOC | an ORM saves |
|---|---|---|
| row → model converters | 70 | ~55 |
| datetime adapters + `_ensure_aware` × 18 | ~40 | ~35 |
| DDL in `init_db` | 97 | ~60 (tables move into model classes, they do not vanish) |
| simple CRUD | ~180 | ~40 — `select(F).where(...)` is not shorter than the SQL |
| migrations | 53 | **−150** — Alembic replaces 53 working lines with a directory, `env.py`, and a versions folder. A net loss at one migration. |
| domain rules: permissions, state transitions, rate limiting, scoring | **~380** | **0** |

Net ~10-15%, for a live-data migration. **Rules out** SQLAlchemy, SQLModel, and Alembic
while this stays single-worker on SQLite. Revisit at multiple workers, where Postgres
forces the question anyway — the SQL here is SQLite-dialect (`PRAGMA`, `INSERT OR REPLACE`)
and would need rewriting regardless. Note DBOS already pulls SQLAlchemy into the tree
(ADR 37), so "a large new dependency" is *not* an argument against it; the LOC maths is.

What actually helped, at a fraction of the risk:

- **`scoring.py`.** Time-weighted Brier and the calibration deciles are ninety lines of
  arithmetic that lived in `db.py` because they read a table. What a forecast is *worth*
  is a methodology question; it is now testable without a database.
- **Two real bugs.** `list_forecasts` issued one query per forecast — 51 round-trips at
  the default limit, now 2. And every question mutation closed its write transaction and
  then called `get_question`, opening a *second* connection: two round-trips with a window
  between them where another writer could land, handing the caller a record that was never
  the result of its own write. `_read_question(conn, ...)` reads inside the transaction.
- **`cast_vote` is one statement.** The table already carried
  `UNIQUE(question_id, ip_hash)`; the select-then-insert-or-update had a TOCTOU window
  where a second vote from the same caller raised an integrity error instead of updating.
- **WAL and `busy_timeout`.** APScheduler's refresh shares a process with the API, so a
  write during a request is ordinary. Without these the loser gets `database is locked`
  immediately rather than waiting.
- **Fresh databases are stamped at `SCHEMA_VERSION`.** Migrations no longer replay against
  a schema born correct — which matters the first time a step *renames* something, since
  that would succeed against a fresh database and corrupt it.

**Related.** `_columns` was dead. `ForecastResearchNotes` had zero references repo-wide.
`_signed` and `_spread` were private aliases nothing used. `_weighted_mean` absorbs three
copies of "weighted mean, None when weightless".

---

## ADR 42 — The CLI is typer; the corpus is JSON

`_build_parser` was 108 lines restating every command's signature a second time, in a
second syntax, a hundred lines from the function that already declared it. Typer takes the
signature from the function. The `_cmd_*` bodies are unchanged — they read flags off an
object, and a `SimpleNamespace` is that object — so this is boilerplate removal, not a
rewrite. The two structural things argparse gave for free are now explicit: mutually
exclusive `--fixture`/`--id` raises a `BadParameter`, as do the enum-ish arguments.

`test_forecasting_baseline/run_baseline.py` was 617 lines, of which **five** were
executable. The other 600 were the 66-question backtest corpus as dict literals — and the
file **could not be imported**: the fixtures called `datetime.date(...)` on a name the file
had bound to the `date` *class*, so `import run_baseline` raised `AttributeError`. Six
hundred lines of data pretending to be code, and broken code at that. The corpus is now
`questions.json` and the module is a 44-line loader that also reports the contamination-risk
split, which is the number that decides which model may see a question at all.

---

## ADR 43 — Every agent call has a ceiling, and every run has a deadline

Two different failures were being confused, and only one of them had a mechanism.

**Acting too often** was handled: `UsageLimits` bounds tool calls and requests, and
`SearchBudget` puts a cline below the wall so a cell converges rather than dying
mid-thought. But five of the eleven `run_agent` call sites passed no limits at all —
`decompose`, `choose_lenses`, `resolution`, `update`, `postmortem` — and fell through to
the process-wide default of **40 requests and 20 tool calls**, a number nobody chose for
them. The critic had the same shape until ADR-less commit `8cd08f3`; the other five kept
it. Each call site now passes explicit limits, and `test_every_agent_run_passes_explicit_limits`
walks the AST of `agents/` to keep it that way. The fallback stays as a backstop for
callers outside the package, but reaching it inside one is now a test failure.

The critic's own budget drops from 3/5 to **2 searches, 3 the wall**. A resolvability
review is one or two lookups — does the source it is about to name exist and publish what
the criteria assume. Anything past that is the critic forecasting the question, which its
prompt already forbids and its budget should not have funded.

**Not acting at all** had no mechanism whatsoever, and it is the failure that actually
stalls. A provider request that never returns reaches no limit, raises nothing, and holds
the run open forever: `execute` never reaches its `finally`, no `end` frame is emitted,
the SSE generator never returns, and the browser spins on a loading state that has no
timeout of its own. Three ceilings close it:

| ceiling | default | bounds |
|---|---|---|
| `AGENT_TIMEOUT_SECONDS` | 180 | one agent run — raises `AgentTimeout` |
| `RUN_TIMEOUT_SECONDS` | 1200 | the whole graph — raises `RunTimeout` |
| `RUN_WATCHER_GRACE_SECONDS` | 45 | how long a run may go unwatched |

All three are `Exception`s reaching `execute`'s catch-all, so every path now ends
`error` → `end`. That pair is the contract the frontend depends on and the only thing
that closes the socket.

**A run no longer outlives its audience.** A run is thirty-odd agent invocations against a
live search budget; finishing one for a tab that closed ten minutes ago spends real money
and shows nobody. A watchdog cancels a run with zero subscribers for the grace window.
The window is not politeness — SSE reconnects, so zero watchers *right now* is a laptop
lid, a proxy hiccup, or a subscriber dropped for falling behind, and all three resolve
themselves. Only zero continuously means gone. `RUN_WATCHER_GRACE_SECONDS=0` restores the
old fire-and-forget behaviour for a deployment that wants it.

**The prompt bug underneath all of this.** `attach_budget_pressure` told an exhausted
agent *"Do not call another tool."* Structured output in pydantic-ai **is** a tool call —
the output schema lives in the toolset — so that sentence forbids the one call that would
end the run. A model that obeys answers in plain text, pydantic-ai replies "please include
your response in a tool call", the instruction is re-fetched per request and says the same
thing again, and the agent burns every request it has without ever producing output. The
budget channels now name *searching* and never tools as a category, and both the
instruction and the tool notice say the final answer is still a tool call.

## ADR 44 — A forecast with no adjudicator is not worth running

`resolution_source` defaulted to `""` on `CreateRunRequest` and was rendered with a red
border and no consequence: the run started anyway. Criteria can be perfectly crisp and
still name nobody who publishes the number that settles them, and unlike every other flaw
this one is invisible until resolution day — the one day it cannot be fixed. The whole run
is spent by then.

Three changes, deliberately at three layers, because each alone is bypassable:

- **The critic must name one.** `suggested_resolution_source` is required by the prompt for
  every question it reviews, whether or not it found anything else wrong.
  `_require_a_source` enforces it in code: a critique naming none is forced to
  `is_resolvable=False` with a finding that says exactly what is absent. Nothing is
  invented — the critic's own suggestion is offered, or the gap is reported.
- **The API refuses without one.** `CreateRunRequest.resolution_source` is `min_length=1`.
- **The UI blocks the button** and offers the critic's suggestion as its own one-click
  action, separate from "apply rewrite" — the two findings are independent, and a question
  with fine criteria and no source has no rewrite to apply.

`ForecastInput` is unchanged. Who adjudicates a question is a property of the run, not
something the agents are asked to reason about.
