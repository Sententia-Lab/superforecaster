# Spec 7 — Run buttons and editable review

**Status: implemented** (August 2026). ADRs 53–55. Closes Project items #25, #28, #29.

Two features that share one piece of machinery.

1. **Run All / Run Section** — a forecast runs end to end from one click. Today a 30-step run
   needs 30 clicks.
2. **Editable review** — a decomposition or a lens set can be corrected before the work below
   it starts, with locks that stop an edit once anything derived from it has run.

Both need the same thing: a function that makes the step rows match what the payloads say.

---

## 1. What problem does this solve?

### Run All

A run is a list of `run_steps` rows. Every row waits for its own click, and the click *is* the
AI call (ADR 46). A 4-sub-question run with 3 lenses each is 30 rows:

```
decompose    1
lenses       4     one per researchable sub-question
base_rates  12     one per (sub-question, lens)
inside_view 12     one per (sub-question, lens)
synthesis    1
```

Thirty clicks, each one waiting for the previous to finish. Nothing about the sequence needs a
human — the gate already refuses anything out of order.

### Editable review

Review gates are advance-only. A decomposition with a wrong sub-question, or a lens set with a
population that does not fit, can only be re-rolled by retrying the step and hoping for
different output. There is no way to correct it by hand. This was deferred in spec5.

---

## 2. Where do these sit in the system?

Run All is **entirely in the browser**. The request is the AI call, so "run everything" is a
loop that opens one stream at a time. No new endpoint, no job queue, nothing running
unattended.

```
RunHeader "Run All"
  -> useRunQueue.drain(scope, run)
       -> runQueue.nextRunnable(run, scope) -> step | null
       -> useStepStream.start(runId, stepId) -> run | null      [the existing SSE endpoint]
       -> repeat with the returned run
```

Editable review adds one endpoint and replaces one function.

```
PUT /runs/{run_id}/steps/{step_id}/payload
  -> machine.edit_payload(run_id, step_id, body)
       -> machine.edit_blocker(step, steps) -> None | str            [409 when set]
       -> agents.decompose.with_ids(body) -> Decomposition          [decompose only]
       -> db.edit_step_payload(step_id, payload_json) -> dict        [writes: run_steps]
       -> machine.reconcile(run_id) -> None                          [writes: run_steps]
  -> machine.detail(run_id) -> dict
  -> 200 {the whole run, steps included}
```

---

## 3. Part 1 — the run queue

### 3.1 Picking the next step

`frontend/src/runQueue.js` — new, pure, no React. `STAGE_ORDER` mirrors `db.STAGE_ORDER`, the
same way `derive.js` mirrors `checks.py`.

```js
export const STAGE_ORDER = ["decompose", "lenses", "base_rates", "inside_view", "synthesis"];

export function nextRunnable(run, scope)   // scope is "all" or one stage name
```

A stage with zero rows is **not** done — its rows have not been created yet. That is the same
rule `advance` used internally, and getting it wrong would let the queue skip a stage that has
not fanned out.

The gate (ADR 45) is enforced in the queue as well as on the server, so a blocked stage stops
the loop instead of producing a 409.

### 3.2 The loop

`frontend/src/hooks/useRunQueue.js` — new.

```js
export function useRunQueue({ stream, run })   // -> { scope, drain, stop }
```

`drain` opens one stream, waits, and uses the returned run as the input to the next round.
`stop` sets a flag and calls `stream.abort()` — disconnecting cancels the in-flight step
server-side, which is ADR 46 working exactly as designed.

A failure stops the queue. The stream emits an `error` frame and no `run` frame, so the loop
has nothing to continue from and exits. Clicking Run All again picks the failed step back up,
because a step in `error` is runnable. One click is one honest attempt at everything
remaining — no hidden retry loop, and no way to spin on a step that always fails.

### 3.3 The frame parser, which had to be fixed first

`start` can only resolve to the run if the `run` frame reaches it, and it never did.
`api.streamStep` split the byte stream on `"\n\n"`, but `sse_starlette` writes **CRLF** — a
raw capture of a real stream contains two `\r\n\r\n` separators and **zero** `\n\n`. The split
therefore never matched, and every frame the backend has sent since spec5 was silently
dropped: no live thought tail, no query line, no source chips.

Nothing looked broken, because `onDone` refetches the run over `GET /runs/{id}` when the
stream closes, so the tree still redrew one step behind. The cost only became visible when the
queue needed the frame's payload to chain one step into the next — and every Run Section
stopped after exactly one step.

The split is now `/\r?\n\r?\n/`, and whatever is still buffered when the reader finishes is
flushed rather than discarded, since the `run` frame is always the last one.

### 3.4 The retry lock, which also had to be fixed

`useStepStream` keeps its `active` state after a failure, so the error card stays on screen:

```js
setActive((a) => (a && a.stepId === stepId && a.error ? a : null));   // useStepStream.js
```

`RunView` derived its "something is running" flag from that same state:

```js
const busy = stream.active !== null;                                  // RunView.jsx
```

So after any failure every Run and Retry button in the run stayed disabled, and the only
recovery was a page reload. `useStepStream` now exposes `streaming` — true only while a
request is in flight — and `busy` reads that instead. The error card still renders; the
buttons come back.

### 3.5 The buttons

**Only the title row sticks.** Resolution criteria run to a full paragraph on a real
question — eight lines is normal — and a sticky block that tall covers the work it is
supposed to sit above. The criteria scroll away; the bar carrying Run All does not. The
title clamps to two lines.

The scroll container's own top padding is a band a stuck bar does not cover, so content
scrolls visibly through it. `.run-header::before` paints that band the page colour, sized
from `--main-pad-top` so it tracks the responsive padding rather than guessing at it.

| Button | Where | Behaviour |
|---|---|---|
| Run All | sticky title row, until the run is complete | Starts a backlog run first (four-field gate unchanged), then drains everything. Becomes **Stop** while running. |
| Run Section | each stage header, when that stage has work left | Runs one stage, then halts for review. Disabled when the gate is not satisfied. |
| per-step Run / Retry | unchanged | unchanged |

### 3.6 What this costs

One AI step runs at a time, process-wide, on purpose — the budget is one person's API key.
Run All does not change that. A 30-step run is 30 sequential calls, on the order of an hour,
with the tab open throughout. Parallelism inside a stage would fix it and reverses ADR 36, so
it needs its own decision. See Deferred.

---

## 4. Part 2 — editable review

### 4.1 The rule

> A payload is editable exactly while everything derived from it is still untouched.

```
decompose  ──derives──►  lenses (one per researchable sub-question)
                              │
lenses[sc1] ──derives──►  base_rates[sc1, each lens]
                              │
                          inside_view[sc1, each lens]  ──►  synthesis
```

| Payload | Editable while… |
|---|---|
| the decomposition | no lens step has run |
| sub-question N's lens set | no base rate **for sub-question N** has run |

The lock is per sub-question. Sub-question 1 locking does not lock sub-question 2.

Because an edit is only ever allowed when nothing downstream has produced anything, cleanup
after an edit can only delete empty pending rows. There is no cascade that destroys hours of
work and no partial-invalidation logic. The lock rule and the cleanup rule are the same rule.

**The lens lock is per-set, not per-lens.** Once any base rate under sub-question 1 has run,
the whole set locks. Weights sum to 1, so changing one lens's weight changes another's — a
per-lens lock would let someone re-weight a measured lens indirectly, which is what ADR 40
exists to prevent.

### 4.2 Why `advance` had to go

`advance` asked *"does this stage have any rows yet?"*:

```python
if complete("lenses") and "base_rates" not in by_stage:      # the old guard
```

That question has the same answer before and after an edit, so `advance` could never react to
one. `expected_steps` asks *"what do the payloads say should exist?"* — a different answer
when a payload changes. That is the whole feature.

### 4.3 `expected_steps` — pure

Every row has one identity, `(stage, sub_claim_id, lens_name)`, which is already the table's
`UNIQUE` key.

```python
def expected_steps(steps: list[dict]) -> set[tuple[str, str, str]]
def _all_complete(steps: list[dict], keys: set[tuple[str, str, str]]) -> bool
```

`expected_steps` walks the five stage **names** in order — not rows — and at each one asks
whether a finished payload lets it compute that stage's rows. It stops at the first stage that
is not fully complete, and never writes.

Two properties matter. `("decompose", "", "")` is always included, so the decompose row is
never stale. And each stage is added *before* its completeness is checked, so pending rows stay
in the result and only rows nobody expects get removed.

`_all_complete` checks keys rather than a stage, which covers both gaps: a row that is pending,
and a row an edit has not created yet.

### 4.4 `reconcile` — the only writer

```python
def reconcile(run_id: str) -> None
```

```
want  = expected_steps(steps)     what run_steps SHOULD contain, computed from payloads
have  = the identities actually in run_steps
stale = have - want               delete
new   = want - have               insert
```

`want` is the **whole table**, finished rows included — it has to be, because it decides what
to delete. If it listed only unfinished work, every completed row would look stale.

Before deleting anything, `reconcile` refuses if a stale row holds work:

```python
kept = [s for s in stale if s["status"] != "pending"]
if kept:
    raise GateError(...)
```

The edit lock makes this unreachable. If a bug ever reaches it, the raise happens before both
writes, so nothing is deleted and nothing is inserted — a 409 instead of quietly discarded
research.

`execute_step` calls `reconcile` where it called `advance`. On the forward path `want` only
grows, so `stale` is always empty and behaviour is identical. **`test_machine.py` passing
unchanged is the regression signal for the swap.**

### 4.5 Data lineage — four traces

`payload_json` is a TEXT column holding a JSON string; it is shown expanded below, with prose
fields elided to `"…"`. Sets of identity tuples have no JSON form, so they appear as arrays.

#### Trace A — the forward path, which must not change

Sub-question 2's lens step just finished. `execute_step` calls `reconcile`.

`steps = db.list_steps("r1")`

```json
[
  {"id": "s0", "stage": "decompose", "sub_claim_id": "", "lens_name": "",
   "status": "complete", "attempts": 1,
   "payload_json": {
     "sub_claims": [
       {"id": "sc1", "question": "Does X reach $500M ARR before 2026-07-01?",
        "knowability": "researchable", "probability": 0.55, "rationale": "…"},
       {"id": "sc2", "question": "Does the IPO window stay open through 2026?",
        "knowability": "researchable", "probability": 0.70, "rationale": "…"}
     ],
     "chain_rule": "conjunction",
     "chain_note": "Both must hold before an S-1 is filed."
   }},

  {"id": "s1", "stage": "lenses", "sub_claim_id": "sc1", "lens_name": "",
   "status": "complete", "attempts": 1,
   "payload_json": {"lenses": [
     {"name": "broad",  "population": "US SaaS firms that crossed $300M ARR since 2015",
      "weight": 0.60, "why_it_fits": "…", "weight_rationale": "…"},
     {"name": "narrow", "population": "US SaaS firms with >80% NDR since 2018",
      "weight": 0.40, "why_it_fits": "…", "weight_rationale": "…"}]}},

  {"id": "s2", "stage": "lenses", "sub_claim_id": "sc2", "lens_name": "",
   "status": "complete", "attempts": 1,
   "payload_json": {"lenses": [
     {"name": "peer", "population": "Quarters since 2010 with 5 or more US tech IPOs",
      "weight": 1.00, "why_it_fits": "…", "weight_rationale": "…"}]}}
]
```

`want = expected_steps(steps)`, built one stage at a time:

```json
[["decompose", "", ""]]
```

s0 is complete, so read its `sub_claims`. Both are researchable, so both get a lens row:

```json
[["decompose", "", ""], ["lenses", "sc1", ""], ["lenses", "sc2", ""]]
```

s1 and s2 are complete, so read their `lenses` arrays:

```json
[["decompose",  "",    ""],
 ["lenses",     "sc1", ""],
 ["lenses",     "sc2", ""],
 ["base_rates", "sc1", "broad"],
 ["base_rates", "sc1", "narrow"],
 ["base_rates", "sc2", "peer"]]
```

`_all_complete` on the three `base_rates` keys is False — no rows carry them — so the walk
stops. No `inside_view`, no `synthesis`.

`have`:

```json
[{"key": ["decompose", "",    ""], "id": "s0", "status": "complete"},
 {"key": ["lenses",    "sc1", ""], "id": "s1", "status": "complete"},
 {"key": ["lenses",    "sc2", ""], "id": "s2", "status": "complete"}]
```

The differences:

```json
{"stale": [],
 "kept":  [],
 "new":   [["base_rates", "sc1", "broad"],
           ["base_rates", "sc1", "narrow"],
           ["base_rates", "sc2", "peer"]]}
```

`db.delete_steps([])` is a no-op; `db.insert_steps` adds three rows:

```json
[{"id": "s3", "stage": "base_rates", "sub_claim_id": "sc1", "lens_name": "broad",
  "status": "pending", "attempts": 0, "payload_json": null},
 {"id": "s4", "stage": "base_rates", "sub_claim_id": "sc1", "lens_name": "narrow",
  "status": "pending", "attempts": 0, "payload_json": null},
 {"id": "s5", "stage": "base_rates", "sub_claim_id": "sc2", "lens_name": "peer",
  "status": "pending", "attempts": 0, "payload_json": null}]
```

Exactly what `advance` produced.

#### Trace B — editing the decomposition

Decompose produced four sub-claims. Three lens rows exist, all pending and empty.

```json
[
  {"id": "s0", "stage": "decompose", "sub_claim_id": "", "lens_name": "", "status": "complete",
   "payload_json": {"sub_claims": [
     {"id": "sc1", "question": "…A…", "knowability": "researchable", "probability": 0.55, "rationale": "…"},
     {"id": "sc2", "question": "…B…", "knowability": "researchable", "probability": 0.40, "rationale": "…"},
     {"id": "sc3", "question": "…C…", "knowability": "judgment",     "probability": 0.50, "rationale": "…"},
     {"id": "sc4", "question": "…D…", "knowability": "researchable", "probability": 0.70, "rationale": "…"}],
     "chain_rule": "conjunction", "chain_note": "…"}},

  {"id": "s1", "stage": "lenses", "sub_claim_id": "sc1", "lens_name": "", "status": "pending", "payload_json": null},
  {"id": "s2", "stage": "lenses", "sub_claim_id": "sc2", "lens_name": "", "status": "pending", "payload_json": null},
  {"id": "s3", "stage": "lenses", "sub_claim_id": "sc4", "lens_name": "", "status": "pending", "payload_json": null}
]
```

`sc3` has no lens row because it is `judgment`.

The request body — delete `…B…`, add a researchable `…E…`. The browser sends no ids:

```json
{"sub_claims": [
   {"question": "…A…", "knowability": "researchable", "probability": 0.55, "rationale": "…"},
   {"question": "…C…", "knowability": "judgment",     "probability": 0.50, "rationale": "…"},
   {"question": "…D…", "knowability": "researchable", "probability": 0.70, "rationale": "…"},
   {"question": "…E…", "knowability": "researchable", "probability": 0.35, "rationale": "…"}],
 "chain_rule": "conjunction", "chain_note": "…"}
```

After `with_ids`, which numbers by position — watch `…D…` move from `sc4` to `sc3`:

```json
{"sub_claims": [
   {"id": "sc1", "question": "…A…", "knowability": "researchable", "probability": 0.55, "rationale": "…"},
   {"id": "sc2", "question": "…C…", "knowability": "judgment",     "probability": 0.50, "rationale": "…"},
   {"id": "sc3", "question": "…D…", "knowability": "researchable", "probability": 0.70, "rationale": "…"},
   {"id": "sc4", "question": "…E…", "knowability": "researchable", "probability": 0.35, "rationale": "…"}],
 "chain_rule": "conjunction", "chain_note": "…"}
```

`want`, `have`, and the differences:

```json
{"want": [["decompose", "", ""], ["lenses", "sc1", ""], ["lenses", "sc3", ""], ["lenses", "sc4", ""]],
 "have": [{"key": ["decompose", "",    ""], "id": "s0", "status": "complete"},
          {"key": ["lenses",    "sc1", ""], "id": "s1", "status": "pending"},
          {"key": ["lenses",    "sc2", ""], "id": "s2", "status": "pending"},
          {"key": ["lenses",    "sc4", ""], "id": "s3", "status": "pending"}],
 "stale": [{"key": ["lenses", "sc2", ""], "id": "s2", "status": "pending"}],
 "kept":  [],
 "new":   [["lenses", "sc3", ""]]}
```

Result:

```json
[{"id": "s0", "stage": "decompose", "sub_claim_id": "",    "lens_name": "", "status": "complete"},
 {"id": "s1", "stage": "lenses",    "sub_claim_id": "sc1", "lens_name": "", "status": "pending"},
 {"id": "s6", "stage": "lenses",    "sub_claim_id": "sc3", "lens_name": "", "status": "pending"},
 {"id": "s3", "stage": "lenses",    "sub_claim_id": "sc4", "lens_name": "", "status": "pending"}]
```

Row `s3` deserves a second look. Its key is `["lenses","sc4",""]` before and after, so
`reconcile` keeps it — but `sc4` named `…D…` before the edit and names `…E…` after. That is
safe **only** because `payload_json` is null: the row holds nothing that was ever true of
`…D…`. Renumbering is safe for the same reason the lock exists. If `s3` had held a finished
lens set, keeping it would attach one sub-question's reasoning to another.

#### Trace C — editing one sub-question's lens set

All lens steps complete, six rows, base rates pending.

```json
[{"id": "s0", "stage": "decompose",  "sub_claim_id": "",    "lens_name": "",       "status": "complete"},
 {"id": "s1", "stage": "lenses",     "sub_claim_id": "sc1", "lens_name": "",       "status": "complete",
  "payload_json": {"lenses": [
     {"name": "broad",  "population": "US SaaS firms that crossed $300M ARR since 2015", "weight": 0.60, "why_it_fits": "…", "weight_rationale": "…"},
     {"name": "narrow", "population": "US SaaS firms with >80% NDR since 2018",          "weight": 0.40, "why_it_fits": "…", "weight_rationale": "…"}]}},
 {"id": "s2", "stage": "lenses",     "sub_claim_id": "sc2", "lens_name": "",       "status": "complete"},
 {"id": "s3", "stage": "base_rates", "sub_claim_id": "sc1", "lens_name": "broad",  "status": "pending", "payload_json": null},
 {"id": "s4", "stage": "base_rates", "sub_claim_id": "sc1", "lens_name": "narrow", "status": "pending", "payload_json": null},
 {"id": "s5", "stage": "base_rates", "sub_claim_id": "sc2", "lens_name": "peer",   "status": "pending", "payload_json": null}]
```

The request body — keep `broad`, replace `narrow` with `recent`:

```json
{"lenses": [
   {"name": "broad",  "population": "US SaaS firms that crossed $300M ARR since 2015",
    "weight": 0.60, "why_it_fits": "…", "weight_rationale": "…"},
   {"name": "recent", "population": "US SaaS firms that crossed $300M ARR since 2022",
    "weight": 0.40, "why_it_fits": "…", "weight_rationale": "…"}]}
```

`SubClaimLensesEdit` accepts it: two lenses is within 1–3, names differ, `0.60 + 0.40 == 1.00`.

```json
{"want": [["decompose", "", ""], ["lenses", "sc1", ""], ["lenses", "sc2", ""],
          ["base_rates", "sc1", "broad"], ["base_rates", "sc1", "recent"],
          ["base_rates", "sc2", "peer"]],
 "stale": [{"key": ["base_rates", "sc1", "narrow"], "id": "s4", "status": "pending"}],
 "kept":  [],
 "new":   [["base_rates", "sc1", "recent"]]}
```

Rows `s3` and `s5` are never touched. Sub-question 2's cell survives an edit to sub-question 1
— the per-sub-question lock falling out of one set difference.

#### Trace D — the guard, if the lock ever leaks

Same as Trace C, except `s4` has been researched:

```json
{"id": "s4", "stage": "base_rates", "sub_claim_id": "sc1", "lens_name": "narrow",
 "status": "complete", "attempts": 1,
 "payload_json": {"lens": {"name": "narrow",
                           "evidence": [{"kind": "counted", "hits": 7, "n": 31, "note": "…"}]},
                  "disagreement": "…"}}
```

`edit_blocker` refuses the edit at the endpoint, so `reconcile` should never see this. If a bug
lets it through:

```json
{"stale": [{"key": ["base_rates", "sc1", "narrow"], "id": "s4", "status": "complete"}],
 "kept":  [{"key": ["base_rates", "sc1", "narrow"], "id": "s4", "status": "complete"}]}
```

```
GateError: cannot discard base_rates step s4 — it is complete
```

Those 7-of-31 counted cases stay in the database.

### 4.6 Deciding whether an edit is allowed

```python
DERIVED: dict[str, Callable[[list[dict], dict], list[dict]]]
def edit_blocker(step: dict, steps: list[dict]) -> str | None
```

`DERIVED` maps an editable stage to the rows that exist because of it. `decompose` derives the
`lenses` rows **and** `synthesis`, because a decomposition with no researchable sub-claims fans
out straight to synthesis. `lenses` derives the `base_rates` rows for its own sub-question
only.

`edit_blocker` returns `None` while the payload may still be edited, and otherwise a sentence
naming what already ran. Saving an edit is then three lines: check the blocker, write the
payload, reconcile.

### 4.7 The endpoint

`PUT /runs/{run_id}/steps/{step_id}/payload`, admin-only like Start and Stream. Returns the
whole updated run so the screen redraws from the response.

| Status | When |
|---|---|
| 404 | that step is not on that run |
| 409 | the run is not active; the step has no payload; something downstream has run |
| 422 | weights do not sum to 1.00; duplicate lens names; fewer than 3 or more than 5 sub-questions |

### 4.8 Request models

The existing models already carry most of the rules, so the edit bodies reuse them.

| Stage | Body | Already enforces | Added |
|---|---|---|---|
| `decompose` | `Decomposition` | 3–5 sub-claims, `ChainRule`, `chain_note` | ids stamped by `with_ids` |
| `lenses` | `SubClaimLensesEdit(SubClaimLenses)` | 1–3 lenses | unique names, Σ weight = 1.00 |

```python
class SubClaimLensesEdit(SubClaimLenses):
    @model_validator(mode="after")
    def _one_whole_judgment(self) -> "SubClaimLensesEdit"
```

Duplicate lens names are not a style problem. A lens is identified by
`(sub-question, name)` in `run_steps` and in `machine._chosen_lens`, so duplicates collide.

Sub-questions are renumbered `sc1…scN` by position. That is safe because the lock guarantees
nothing points at the old numbers yet, and `db.list_steps` sorts by
`(stage index, sub_claim_id, lens_name)` with no creation-order key — so deleting and
reinserting rows does not disturb display order.

### 4.9 Recording that a human wrote it

New column `run_steps.edited_at`, schema version 3. Four places change together: `SCHEMA_VERSION`,
`MIGRATIONS[3]`, the `CREATE TABLE run_steps` block, and `_row_to_step`.

`init_db` stamps a fresh database at `SCHEMA_VERSION` before `_migrate` runs, so migration 3
never replays against a table born with the column — which matters, because `_migrate` only
tolerates `no such column` and a replay would raise `duplicate column name`.

A payload a person wrote is different evidence from one the AI produced, and this system keeps
such differences visible. It surfaces as an "edited" chip on the card. `status` and `attempts`
do not move — the step is still complete, and an edit is not an attempt.

### 4.10 Weights that sum to 1

Weights were relative. Every consumer divides by `Σw` (`checks._weighted_mean`), so only ratios
ever mattered. They now sum to 1, enforced differently on each side:

```
AI output      0.9   0.6   0.4        Σ 1.9
               ──────────────────────────────────
  stored as    0.47  0.32  0.21       Σ 1.00     ratios 2.25 : 1.5 : 1 — identical
  three-way    0.33  0.33  0.34       Σ 1.00

your edit      0.55  0.45             Σ 1.00     ✓ saved
your edit      0.55  0.50             Σ 1.05     ✗ 422, Save disabled
```

```python
def normalize_weights(lenses: list[Lens]) -> list[Lens]
```

- **AI output is rescaled on the way in**, not rejected. The type the AI returns is the type
  stored, so a strict validator would make the agent retry against arithmetic it has no reason
  to hit. Rescaling preserves every ratio, and every consumer divides by the sum, so **no
  computed number moves.**
- **Human edits must already sum to 1.00.** Rewriting numbers somebody typed would hide the
  constraint rather than teach it.
- **Existing runs keep their raw weights.** No backfill. The math is unaffected; only the
  displayed numbers differ between old and new runs.

Largest-remainder rounding at two decimals, floored at `0.01` because `Lens.weight` is `gt=0.0`
— a weight of exactly zero cannot be constructed. Applied in `run_lenses_stage`, where the lens
step returns. `run_base_rate_step` already re-stamps `weight` from the chosen lens onto the
researched lens, so the normalized value carries through unchanged.

This also matches the eval harness in `spec/planned/spec6.md`, whose `score_lenses` scorer
expects weights to sum to about 1.

### 4.11 The editors

`derive.js` gains the two mirrors it needs, following that file's existing job:

```js
export function editBlocker(step, steps)      // mirrors machine.edit_blocker
export function normalizeWeights(lenses)      // mirrors normalize_weights, for the button
```

Both editors are built from `EditorField`: one labeled field per row, full width, multi-line
by default. The long fields here hold sentences or paragraphs — a sub-question, a population
definition, an argument for a weight — and a single-line input truncates every one of them at
the same width, which makes a payload impossible to review while editing it. The box grows
with its text through CSS `field-sizing: content` rather than a measured `scrollHeight`;
measuring needs settled layout, and reading it a frame early stores a garbage height that
then sticks until the next keystroke. Each sub-question or lens is one `.editor-block`,
separated by the same dashed rule the evidence rows use.

**`DecomposeEditor`** — question, rationale, and researchable-vs-judgment on every row, plus a
`probability` input **on judgment rows only**. The `scN` on each block is live rather than
stored: ids are re-stamped by position on save, so removing the second sub-question renumbers
everything below it, and showing that while editing is more honest than showing ids that are
about to change. `checks.chain_inputs` reads a sub-claim's own
probability only when nothing researched it:

```python
researched = sub_claim_rate(s.id, o, i) if s.id else None
rows.append({"rate":   researched if researched is not None else s.probability,
             "source": "researched" if researched is not None else "estimated"})
```

A researchable sub-claim takes its rate from its lenses, so a number typed there is discarded.
A judgment sub-claim has no lenses, so this is its only contribution to the anchor — and
omitting it would make a conjunction treat that column as `1.0`. The field appears where it is
used, labelled as a working estimate.

**`LensSetEditor`** — name, population, why it fits, weight, weight rationale. A live Σ chip
that stays red until it reads `1.00`, a **Normalize** button, and Save disabled until it is
exactly `1.00`.

---

## 5. What changes, file by file

| File | Change |
|---|---|
| `backend/superforecaster/db.py` | `SCHEMA_VERSION = 3`, `MIGRATIONS[3]`, `edited_at` in the create block and `_row_to_step`; new `delete_steps`, `edit_step_payload` |
| `backend/superforecaster/machine.py` | `advance` → `expected_steps` + `_all_complete` + `reconcile`; new `DERIVED`, `edit_blocker`, `edit_payload` |
| `backend/superforecaster/models.py` | `SubClaimLensesEdit`; `edited_at` on `RunStepOut` |
| `backend/superforecaster/stages.py` | `normalize_weights`, applied in `run_lenses_stage` |
| `backend/api/runs.py` | `PUT /runs/{run_id}/steps/{step_id}/payload` |
| `frontend/src/runQueue.js` | new — `STAGE_ORDER`, `nextRunnable` |
| `frontend/src/hooks/useRunQueue.js` | new — `drain`, `stop` |
| `frontend/src/components/RunHeader.jsx` | new — sticky title row, Run All / Stop; the criteria scroll |
| `frontend/src/components/EditorField.jsx` | new — one labeled, wrapping field per row |
| `frontend/src/components/DecomposeEditor.jsx` | new |
| `frontend/src/components/LensSetEditor.jsx` | new |
| `frontend/src/hooks/useStepStream.js` | `start` returns the last run; new `streaming` flag |
| `frontend/src/derive.js` | `editBlocker`, `normalizeWeights` |
| `frontend/src/api.js` | `editStepPayload` |
| `frontend/src/components/RunView.jsx` | queue wiring, Run Section buttons, Edit pencils, lock and edited chips |
| `frontend/src/theme.css` | Σ chip, lock chip, sticky header |

---

## 6. Verification

```bash
cd backend && ADMIN_API_KEY="" uv run pytest
cd frontend && npm ci && npm run build
```

The empty `ADMIN_API_KEY` is required while the test suite is not isolated from `backend/.env`
— a known issue, and this spec adds another admin route that hits it.

1. `test_machine.py` passes unchanged — the safety net for replacing `advance`.
2. `reconcile` adds missing rows, deletes stale pending ones, and refuses to delete a row that
   holds work.
3. Editing: rows re-key per sub-question; 409 once anything downstream has run; 422 on Σ ≠ 1,
   duplicate names, and 2 or 6 sub-questions; `edited_at` set with `status` and `attempts`
   unmoved; an edited weight reaches the base-rate step that consumes it.
4. Weights: Σ = 1.00 exactly, ratios preserved, three equal lenses land `0.33 / 0.33 / 0.34`, a
   lopsided set floors at `0.01`.
5. Migration v3 applies to an existing database and leaves a fresh one alone.

By hand: Run All drains a new forecast with no further clicks; Stop leaves the step cancelled
and re-runnable; a forced failure halts the queue and leaves the buttons live; editing a
decomposition redraws the lens section; a Σ of 1.05 blocks Save until Normalize; running one
base rate locks that sub-question's lenses and no others; reload keeps edits and locks.

---

## 7. Deferred

- **Wall clock** — 30 sequential AI calls. Parallelism inside a stage reverses ADR 36 and needs
  its own decision.
- **Editing base rates and inside-view adjustments** — the same rule generalizes; `DERIVED`
  takes two more entries.
- **Writing a step by hand instead of running it** — saving a payload onto a *pending* step
  would skip the AI and close Project item #26, where a permanently failing cell blocks its run.
- **`Forecast.decompositions[].probability` is carried, not computed** — synthesis is handed
  the decomposition JSON including pre-research working estimates and told to carry the
  sub-claims through. `check_linkage` verifies the ids survive; nothing compares the
  probabilities to `chain_inputs`. A saved forecast can therefore show a pre-research guess
  against a sub-question that was measured. The fix mirrors `ResearchedLens`, which has no
  `base_rate` field because the rate is computed from evidence. Own spec and own ADR.
- **Backfilling old lens weights** — existing runs keep weights that do not sum to 1.
- **No frontend test runner** — `runQueue.js` and the `derive.js` helpers are pure so they are
  testable the day one exists.
