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

### Single AI Agent, Multi-Step Reasoning

**Decision:** One Pydantic AI agent that runs decomposition, research, and synthesis in a single structured call.

**Rationale:** The v2 implementation split these into separate prompts whose outputs were ignored. A single structured call with `output_type=Forecast` forces the model to commit to a complete, coherent forecast in one pass. This is simpler, faster, and produces better-integrated reasoning.

**What this rules out:** Separate specialized sub-agents (Decomposer, Researcher, Synthesizer). We may revisit this as complexity grows, but the overhead of chaining agents is not justified yet.

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

### Three Agents, One Tool Set

**Decision:** Three separate Pydantic AI `Agent` instances, each with a distinct task, output type, and system prompt. All three share the same tool functions (`search_web`, `search_wikipedia`) imported from `tools.py`.

```
superforecaster/tools.py      → search_web(), search_wikipedia()  (shared)
superforecaster/agent.py      → forecast_agent    output_type=Forecast
superforecaster/refresh.py    → refresh_agent     output_type=ForecastRefreshResult
superforecaster/resolution.py → resolution_agent  output_type=ResolutionCheckResult
```

**Rationale:** `output_type` is set at `Agent` construction time in Pydantic AI — a single instance cannot produce two different structured types. More importantly, the three tasks are genuinely different in nature and stakes:

- **forecast_agent**: produces an initial structured belief from scratch — decomposition, base rate research, synthesis
- **refresh_agent**: interrogates whether an existing probability should change given new evidence — a narrower, update-focused task
- **resolution_agent**: determines whether the underlying event has already occurred — a binary classification task against the stated resolution criteria

Resolution detection is separated from probability refreshing because their **consequences are asymmetric**. A wrong probability update is low-risk: the time-weighted Brier score absorbs it across the full horizon. A wrong resolution call permanently closes the forecast. That asymmetry justifies dedicated focus — an agent that only asks "has this resolved?" with no other job competing for its attention.

**Daily run order:**
```
1. resolution_agent  → flags appears_resolved → admin reviews before any close
2. refresh_agent     → probability update on non-flagged active forecasts
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
  superforecaster/          # Core package (flat — no sub-packages)
    models.py               # All Pydantic models
    tools.py                # search_web, search_wikipedia (shared by all agents)
    agent.py                # forecast_agent    — output_type=Forecast
    refresh.py              # refresh_agent     — output_type=ForecastRefreshResult
    resolution.py           # resolution_agent  — output_type=ResolutionCheckResult
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

**Rationale:** Separating `backend/` and `frontend/` at the top level makes the repo self-explanatory — Python and Node.js code never share a directory. Each has its own `Dockerfile` colocated with the code it builds. `docker-compose.yml` stays at root because it is the one file that knows about both services. Within each service, the flat-file principle applies: modules are files, not nested packages.

---

### Forecast Updates Are Driven by a Daily Refresh Job, Not Events

**Decision:** Once per day, a scheduled job re-runs the agent on every active (unresolved, non-ambiguous) forecast to check for new evidence. If the agent determines its probability should change by ≥ 3 percentage points, a new `forecast_update` row is written. No other mechanism triggers updates.

**Rationale:** Superforecasting principle 10 ("frequent, small updates") requires the agent to revisit beliefs as evidence arrives — but we need a disciplined cadence, not ad-hoc re-runs. Daily is frequent enough to catch meaningful developments and cheap enough to run on every open forecast. The 3-point threshold filters out noise from the agent slightly rephrasing the same view.

**What this rules out (for now):**

- Event-driven updates triggered by specific news or webhooks
- User-initiated re-runs of the agent
- Automatic updates on any other schedule

The architecture should make it easy to add event-driven triggers later — the `refresh_forecast(id)` function should be callable from any trigger, not just the cron job. But we will not build the event layer until the daily batch is running and validated.

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