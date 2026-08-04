# Technical Direction

Architecture decisions, desired features, and justifications. This is the technical roadmap set by the project lead — less about implementation details, more about why we're building what we're building.

---

## What We're Building

An open-source, crowd-sourced forecasting platform. The community submits and votes on forecast questions. The admin vets and triggers an AI agent that applies rigorous superforecasting methodology. Results are public, with full reasoning and accuracy tracking over time.

This is not just a chatbot that gives confident-sounding answers. It is a calibration machine — the goal is probabilistic accuracy that can be measured and improved.

---

## Why Tetlock's Superforecasting Methodology

Most AI forecasting tools produce confident-sounding outputs with no accountability. Superforecasting is different:

- **Calibration is measurable.** We can track whether our 70% forecasts actually occur 70% of the time. This makes quality visible.
- **Reasoning is transparent.** Every forecast shows its decomposition, base rates, and evidence — not just a number.
- **It improves over time.** Post-mortems on resolved forecasts feed back into the system. The agent should get better as the dataset grows.
- **It handles uncertainty honestly.** A well-calibrated 60% forecast is more valuable than a false 90%.

The methodology is codified in `spec/superforecasting_methodology.md`. Every major feature decision should be traceable back to a principle in that document.

---

## Architecture Decisions

### One Agent Per Methodology Step

**Decision:** One Pydantic AI agent per step of the superforecasting methodology, each with its own `output_type` and system prompt. Five are orchestrated by a graph; three stand alone.

**Rationale:** A step you cannot run in isolation is a step you cannot test. The earlier single-call design made every principle a claim about a prompt rather than a property of an output — there was no way to ask "does it find base rates" without running the whole pipeline against a live LLM and reading the prose.

Splitting gives every step a named entry point (`run_decompose`, `run_outside_view`, …) a test can call with fixed inputs. It also narrows each `output_type` enough that Pydantic validation does real work: `OutsideView` with `reference_classes: Field(min_length=2)` structurally guarantees principle 7 in a way no prompt can.

**What this replaces:** This reverses the earlier "Single AI Agent, Multi-Step Reasoning" decision. That decision was made when the problem was v2 ignoring its own prompt outputs — a real problem, correctly diagnosed. The problem now is that nothing is measurable, and the single-call shape is what makes it unmeasurable.

---

### Pydantic AI as the Agent Framework

**Decision:** Continue using Pydantic AI. Do not switch to LangChain, LlamaIndex, or raw API calls.

**Rationale:** Structured outputs (`output_type=Forecast`) are the core requirement — the agent must return a typed `Forecast` object, not freeform text. Pydantic AI handles this natively with type safety and validation. The alternative frameworks add abstraction without solving our core problem better.

---

### SQLite for Persistence

**Decision:** Store forecasts and community questions in a local SQLite database.

**Rationale:** Simple, zero-infrastructure, queryable with SQL. A deployed instance can use a single `.db` file on disk. If we scale to production traffic that warrants it, the schema can migrate to PostgreSQL without changing application logic. We will not introduce a database server until SQLite is a demonstrated bottleneck.

---

### FastAPI for the REST Layer

**Decision:** FastAPI as the backend API framework.

**Rationale:** Native async support matches Pydantic AI's async model. Auto-generated OpenAPI docs (`/docs`) are essential for the community and frontend integration. Pydantic model sharing between the agent and API response schemas reduces duplication.

---

### Next.js on Vercel for the Frontend

**Decision:** Next.js (App Router), deployed to Vercel.

**Rationale:** Vercel is the fastest path to a public URL with zero infra management. Next.js gives us server-side rendering for forecast detail pages (SEO matters for public forecasts). TypeScript throughout.

---

### API Key Auth for Admin Actions

**Decision:** Admin endpoints protected by a static Bearer token (`ADMIN_API_KEY` env var). No OAuth, no user accounts.

**Rationale:** This is a single-admin platform (operated by Naren). Full OAuth is unnecessary complexity. If multi-admin is needed later, we add it then.

---

### Community Questions Are Submitted Openly, Tracked by IP

**Decision:** Anyone can submit a question or vote — no account required. Rate limiting and vote deduplication are enforced by hashed IP address. IPs are stored hashed (SHA-256), never in plaintext.

**Rationale:** Friction kills community contribution. The admin review step is the quality gate, not registration. IP-based tracking gives just enough friction to prevent obvious spam (1 submission per IP per 24h, 1 vote per IP per question) without requiring user accounts. Hashing protects privacy while still enabling deduplication.

**Vote semantics:** Upvote (+1) or downvote (−1). A user can switch their vote or undo it entirely. The questions list sorts by net vote score (upvotes − downvotes). This is more expressive than upvote-only and surfaces genuine community preference.

---

### MUI (Material UI) for the Frontend Component Library

**Decision:** Use MUI (v6) as the React component library. No Tailwind.

**Rationale:** MUI components are standardized across the entire UI — forms, tables, dialogs, chips, badges. This keeps the frontend visually consistent without building a design system from scratch. MUI's theming system handles dark mode cleanly. Tailwind is excluded because mixing utility classes with MUI components creates specificity conflicts and inconsistent results.

---

### Eight Agents, One Tool Set

**Decision:** Eight `Agent` instances, one per methodology step, sharing the tool functions in `tools.py`. Agents know nothing about each other; sequencing lives in `graphs/`.

```
superforecaster/tools.py          → search_web(), search_wikipedia(),
                                    find_disconfirming_evidence()      (shared)
superforecaster/agents/
  decompose.py     P1, P2       → Decomposition
  outside_view.py  P4, P7       → OutsideView
  inside_view.py   P5,9,14,15   → InsideView
  synthesize.py    P6, P8, P16  → Forecast
  resolution.py                 → ResolutionCheckResult
  update.py        P10,11,12    → UpdateDecision
  critic.py        P3           → CriteriaCritique      (standalone)
  postmortem.py    P13          → PostMortem            (standalone)
```

**Rationale:** `output_type` is fixed at construction, so one instance cannot produce two structured types. Beyond that mechanical reason, each step has a different job and different stakes, and separating them is what makes each testable in isolation.

Resolution detection stays separate from probability updating because their **consequences are asymmetric**. A wrong probability update is low-risk: the time-weighted Brier score absorbs it across the horizon. A wrong resolution call permanently closes the forecast. That asymmetry justifies an agent that only asks "has this resolved?" with nothing else competing for its attention — and the prompt is written to bias toward the cheap error.

`critic.py` and `postmortem.py` sit outside both graphs deliberately. The criteria critic runs while a question is still being drafted, which is the only point at which fixing ambiguous criteria is cheap. The post-mortem runs after resolution.

---

### Pydantic Graphs for Orchestration

**Decision:** Use `pydantic_graph` for the forecast pipeline and the daily update cycle. Orchestration lives in `superforecaster/graphs/`.

```
forecast:  Decompose → FindBaseRates → AdjustInsideView → Synthesize → Critique
                                                              ↑            ↓
                                                              └────────────┘
                                                       (blocking violation, once)

update:    CheckResolved ──resolved──→ End(flagged)
                 └─not resolved──→ ApplyBayes → GuardUpdate ⇄ VerifyLargeMove → End
```

**Rationale:** Both pipelines have real control flow, not just a sequence. More importantly, the ordering becomes structural. Principle 4 says "outside view first" — as a prompt instruction that is a hope; as the edge `FindBaseRates → AdjustInsideView` it cannot be violated, because the inside-view agent takes the base rate as an argument.

The same applies to the daily cycle. "Resolution blocks the probability update" used to be a `flagged_ids` set passed between two `for` loops. It is now an unreachable node.

`Graph.mermaid_code()` renders the real wiring, so the diagrams in the specs cannot drift from the code.

**What this rules out:** Hand-rolled orchestration.

---

### Methodology Checks Are Pure Functions, Not Prompts

**Decision:** The checkable principles live in `superforecaster/checks.py` as pure functions over Pydantic models, returning `CheckViolation | None`. Every threshold is an env-tunable value in `config.CheckThresholds`.

**Rationale:** A principle stated in a prompt cannot be tested. A function over structured output can be unit tested in microseconds. `check_bayes_direction` verifies the probability moved the same direction as the agent's own stated likelihood ratios — arithmetic, which either holds or does not.

They are also a runtime feedback loop, not just a test fixture: the `Critique` node runs them and routes failures back to synthesis with the specific violation attached, so the retry is a correction rather than a re-roll.

**What this rules out:** LLM-as-judge for these principles. A judge model is slower, costs money, is non-deterministic, and is no more correct than a comparison operator.

---

### Two Clamps for Contamination-Free Backtesting

**Decision:** Scoring the agent against resolved questions clamps both the tools and the model. Tools return nothing published after the question's `asked_at`; the model must have a training cutoff earlier than `asked_at`. `model_garden.pick_clean_model` returns `None` rather than falling back.

**Rationale:** Contamination has two doors. Clamping only the tools leaves the model reciting an outcome it memorised in training — it already knows how 2022 went. Clamping only the model leaves it reading a 2024 article about a 2022 question. Both have to shut or the score is fiction.

**What this rules out:** Treating the existing 66-question set as a benchmark. Measured against the garden it gives `0/66` clean coverage — the earliest served training cutoff is Jul 2025 and the newest of those questions was asked Sep 2024. A backtest needs questions asked after roughly Oct 2025, which is why the end-to-end harness is deferred to `spec/change_specs/spec4.md` pending a suitable corpus. The clamps themselves ship, and the garden's reach grows on its own as models age.

**Daily run order** (now a graph, not a convention):
```
CheckResolved → flags appears_resolved → admin reviews before any close
              → if not flagged, ApplyBayes updates the probability
```

---

### Docker for Local Development and Deployment

**Decision:** The full stack (FastAPI backend + Next.js frontend) runs via `docker compose up`. SQLite data is persisted in a named volume. No separate database container.

**Rationale:** Docker removes environment setup friction — any contributor or deployment target gets an identical runtime. SQLite needs no sidecar container, which keeps the compose file simple (two services: `api` and `frontend`). The same `docker-compose.yml` is used for local testing and can be dropped onto any container host (Railway, Render, Fly.io) for production.

---

### Flat, Modular Code Structure

**Decision:** No deep directory nesting. All Python modules sit directly in their package. API routers are flat files, not nested subdirectories.

**Target layout:**
```
backend/                    # All Python code
  superforecaster/          # Core package
    models.py               # All Pydantic models
    tools.py                # search_web, search_wikipedia, find_disconfirming_evidence
    checks.py               # Pure methodology validators
    deps.py                 # ForecastDeps — the two contamination clamps
    model_garden.py         # Model registry keyed by training cutoff
    agents/                 # One module per methodology step (8)
    graphs/                 # Orchestration only — forecast.py, update.py, state.py
    evals/                  # Component test harness + per-agent golden data
    db.py                   # All DB operations
    cron.py                 # Scheduled jobs
    __main__.py             # CLI
    fixtures/               # JSON test fixtures for manual agent CLI testing
  api/                      # FastAPI layer (flat — no sub-packages)
    main.py                 # App + lifespan
    forecasts.py            # Router
    questions.py            # Router
    calibration.py          # Router
    admin.py                # Router
    deps.py                 # Shared dependencies
  pyproject.toml
  uv.lock
  Dockerfile                # Builds the API image; context is backend/

frontend/                   # Next.js app
  app/
    page.tsx                # /  — Submit & Vote
    predictions/
    resolved/
    forecasts/[id]/
    admin/
  components/               # Shared MUI components
  lib/                      # API client, utils
  Dockerfile                # Builds the frontend image; context is frontend/

docker-compose.yml          # Repo root — orchestrates both services
.env.example                # Repo root — all env vars for both services
```

**Rationale:** Separating `backend/` and `frontend/` at the top level makes the repo self-explanatory — Python and Node.js code never share a directory. Each has its own `Dockerfile` colocated with the code it builds. `docker-compose.yml` stays at root because it is the one file that knows about both services.

Within the package, modules are files rather than nested packages, with two exceptions: `agents/` and `graphs/`. Eight agents as eight flat files would bury the shared modules they sit beside, and the split is the point of the architecture — orchestration in one directory, agents in another, each ignorant of the other. `evals/` follows because it carries data files alongside its code.

---

### Forecast Updates Are Driven by a Daily Refresh Job, Not Events

**Decision:** Once per day, a scheduled job re-runs the agent on every active (unresolved, non-ambiguous) forecast to check for new evidence. If the agent determines its probability should change by ≥ 3 percentage points, a new `forecast_update` row is written. No other mechanism triggers updates.

**Rationale:** Superforecasting principle 10 ("frequent, small updates") requires the agent to revisit beliefs as evidence arrives — but we need a disciplined cadence, not ad-hoc re-runs. Daily is frequent enough to catch meaningful developments and cheap enough to run on every open forecast. The 3-point threshold filters out noise from the agent slightly rephrasing the same view.

**What this rules out (for now):**

- Event-driven updates triggered by specific news or webhooks
- User-initiated re-runs of the agent
- Automatic updates on any other schedule

The architecture should make it easy to add event-driven triggers later — `run_update_graph(id)` is callable from any trigger, not just the cron job. But we will not build the event layer until the daily batch is running and validated.

---

## Desired Features (Priority Order)

### 1. Real LLM Decomposition (Fixes Critical Bug)

The agent must use LLM output for decomposition and research, not hardcoded mocks. This is the most important fix — nothing else matters until the core loop works correctly.

### 2. Forecast Persistence and Calibration Tracking

Every forecast should be saved. When outcomes are known, record them. Compute Brier scores and calibration by probability bucket. This is the feedback loop that makes the system valuable over time.

### 3. Community Question Platform

Public submission, upvoting, and admin curation. Top-voted questions each month should surface for admin review. The admin triggers forecasts after vetting.

### 4. Public Web UI

A minimal, readable frontend showing forecasts, full reasoning, and calibration stats. Community can browse and vote. No login required for public features.

### 5. Monthly Digest

An automated job that promotes top-voted questions to "approved" status at month-end, so the admin can trigger forecasts with one click rather than manually reviewing everything.

### 6. Daily Forecast Refresh

A scheduled job that runs the agent once per day on every active forecast, searches for new evidence, and writes a new probability update if the change exceeds the minimum threshold. This is what makes the system a living calibration machine rather than a set-and-forget tool.

---

## What's Out of Scope (For Now)

- **User accounts / authentication** beyond the single admin API key
- **Real-time updates** (WebSockets, SSE) — polling is fine for a low-frequency use case
- **Multiple AI models or model comparison** — one good forecast is better than three mediocre ones
- **Mobile app** — responsive web is sufficient
- **Comments/discussion threads** on forecasts — voting is the initial social feature; comments add moderation complexity
- **Automatic forecasting** without admin approval — the human review step is intentional

---

## Design Principles

**Calibration over confidence.** The agent should be accurate and honest about uncertainty, not impressive-sounding.

**Every forecast is auditable.** Full reasoning, decomposition, and research shown publicly. No black box.

**Iterate on real data.** Post-mortems on resolved forecasts are how this system improves. Build for learning, not just output.

**Keep it simple until it needs to be complex.** Single file → package → API → UI. Do not abstract ahead of need.