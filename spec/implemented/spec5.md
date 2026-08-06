# Spec 5 — The gated rebuild

**Status: implemented** (August 2026). ADRs 45–48.

## Why

The app had spun out: the frontend broke on reload, agents ran unattended for 20 minutes
through an in-memory ring buffer with a second database (DBOS) checkpointing them, and a
watchdog killed runs 45 seconds after the last viewer left while the UI claimed the
opposite. This spec replaced the orchestration and the frontend while preserving the math
(`checks.py`, `scoring.py`), the models, and the ten agents untouched.

## The product

1. **Left sidebar** — Backlog / Running / Complete, from `GET /runs`. Errored runs carry a
   red chip (`gated_runs.error`).
2. **Main pane** — blank until a selection:
   - a complete run → the full persisted tree, read-only;
   - an active run → the gated pipeline;
   - a backlog run → editable fields + Start;
   - New forecast → freeform text → AI draft (question, criteria, date, source) → "Run
     now" (gated on all four fields) or "Add to backlog". The AI draft fills the fields,
     so it can be run as-is; a hand-typed forecast stays gated until complete.
3. **Every stage is user-gated.** All agents stop unless the user clicks next:
   1. **Decompose** — 3–5 sub-questions. The only stage with a raw live tail.
   2. **Find lenses** — per sub-question, 1–3 lenses each. Runnable per sub-question;
      base rates stay locked until every sub-question has lenses.
   3. **Base rates** — per (sub-question, lens) cell; a counted rate (Σhits/Σn).
   4. **Inside view** — per cell; modifiers with signed magnitudes.
   5. **Synthesis** — anchor and implied probability computed by `checks` (not agentic),
      then reflect + synthesize + pure critique with one retry. The stated probability
      may deviate from implied by at most ±`CHECK_DERIVATION_SLACK` (default 0.05 — the
      ±5-point rule), enforced by `check_derivation` and displayed as data.

Removed: concurrent runs, endless streaming, DBOS, community features (ADR 48).

## The machine (ADR 45)

- `gated_runs` (status `backlog|active|complete`, nullable `error`) and `run_steps`
  (status `pending|running|complete|error`, `payload_json`, UNIQUE(run_id, stage,
  sub_claim_id, lens_name)) in the one SQLite database; migration v2 drops `questions`,
  `votes`, `refresh_runs`, `runs`.
- Payloads: decompose → `Decomposition`; lenses → `SubClaimLenses`; base_rates →
  `BaseRateStepPayload` (re-stamped lens + disagreement + sources); inside_view →
  `InsideStepPayload` (stamped adjustments + steel man + sources); synthesis →
  `SynthesisStepPayload` (reflection, both views, forecast, violations, anchor, implied,
  slack, attempts).
- `machine.py` decides transitions: `start_run` (four-field gate), `advance`
  (materializes the next stage's pending rows from the just-written payloads; zero
  researchable sub-questions bypass straight to synthesis), `execute_step` (claim CAS →
  dispatch under `STAGE_TIMEOUT_SECONDS` → persist result / error / `cancelled`).
- `stages.py` holds the per-stage functions with the graph's code-stamped invariants
  ported: lens identity re-stamped from the *chosen* lens; `lens_name`/`sub_claim_ids`
  stamped by code; lenses chosen blind; adjust requires a measured rate by signature.
  `stages.run_all` drives them gate-free for the CLI (`superforecaster forecast`) and
  evals.
- One agent step in flight per process. Iteration budgets unchanged: soft/hard search
  budget per cell (`get_cell_budget`), explicit `UsageLimits` on every agent call
  (AST-enforced by `test_critic_budget`), 180s per agent call, 600s per step.

## The API

| Endpoint | Purpose |
|---|---|
| `POST /runs` | create (fields optional) → backlog |
| `GET /runs` | sidebar list with stage counts |
| `GET /runs/{id}` | full tree — the reload path |
| `PATCH /runs/{id}` | edit while backlog (409 otherwise) |
| `DELETE /runs/{id}` | remove run (cascades steps) |
| `POST /runs/{id}/start` | four-field gate → active + pending decompose (422 names missing fields) |
| `POST /runs/{id}/steps/{step_id}/stream?max_iterations=N` | **the gated "next"** — SSE that *is* the step (ADR 46); disconnect cancels; 409 on gate/busy/double-claim |
| `POST /questions/draft`, `/critique` | the AI-draft flow (kept) |

## The frontend (ADR 47)

React 18 + Vite in `frontend/` (`npm run build` → `dist/`, served by FastAPI).
`src/derive.js` mirrors `checks.py`; `src/theme.css` carries the ported `--pv-*` tokens;
`useStepStream` ties an AbortController to component lifetime. Layout contract: stages
stack vertically; decompose is the only raw tail; sections 2–4 render card headlines up
front with processing inside the active card and results at the bottom; synthesis shows
the arithmetic table (counted → adjusted → weighted → chain → implied via `derive.js`),
the final probability, rationale, reflection, and surviving violations.

## Tests

`test_db_gated_runs` (payload round-trips, claim CAS, restart sweep, migration v2),
`test_machine` (materialization fan-out, gate enforcement, retry, cancel, stage timeout,
global slot), `test_api_gated_runs` (CRUD statuses, stream frame order, cancel-on-
disconnect), `test_cli_autoadvance` (`run_all` ordering + degraded cells). CI runs
`uv run pytest` + `npm run build` on push/PR; a checked-in pre-push hook runs pytest.

## Deferred (flagged, not forgotten)

- **Editable review**: review gates are advance-only; editing a decomposition or lens
  before advancing needs payload-edit endpoints and downstream invalidation.
- **Skip-a-failing-cell**: strict gating means a permanently failing cell blocks its run;
  retry (optionally with `?max_iterations=` up to 50) is the only escape hatch today.
