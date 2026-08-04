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
