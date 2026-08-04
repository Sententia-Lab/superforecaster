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
| [4](#adr-4--nextjs--mui-for-the-frontend) | Next.js + MUI for the frontend | Accepted |
| [5](#adr-5--api-key-auth-for-admin-actions) | API key auth for admin actions | Accepted |
| [6](#adr-6--open-submission-tracked-by-hashed-ip) | Open submission, tracked by hashed IP | Accepted |
| [7](#adr-7--time-weighted-brier-scoring) | Time-weighted Brier scoring | Accepted |
| [8](#adr-8--docker-for-local-dev-and-deployment) | Docker for local dev and deployment | Accepted |
| [9](#adr-9--daily-batch-updates-not-event-driven) | Daily batch updates, not event-driven | Accepted |
| [10](#adr-10--a-single-agent-in-one-structured-call) | A single agent in one structured call | **Superseded by 11** |
| [11](#adr-11--one-agent-per-methodology-step) | One agent per methodology step | Accepted |
| [12](#adr-12--pydantic-graphs-for-orchestration) | Pydantic graphs for orchestration | Accepted |
| [13](#adr-13--methodology-checks-are-pure-functions) | Methodology checks are pure functions | Accepted |
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

**Known cost.** `POST /forecasts` runs a full graph synchronously inside the request. The HTTP
response blocks for the entire run. Accepted for now; an async job layer is not built.

---

## ADR 4 — Next.js + MUI for the frontend

**Status:** Accepted (v3)

**Decision.** Next.js App Router on Vercel, MUI v6, TypeScript throughout. No Tailwind.

**Rationale.** SSR matters because public forecast pages should be indexable. MUI gives a
consistent component set without building a design system.

**Rules out.** Tailwind — mixing utility classes with MUI creates specificity conflicts.

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

**Decision.** `docker compose up` runs the full stack. Two services, `api` and `frontend`.
SQLite in a named volume, no database container.

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
