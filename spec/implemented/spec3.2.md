# Replace self-reported `confidence` with graded sources; make P16 advisory; make the trail legible

## Context

A live run produced this violation:

```
Principle 16 (calibration_hygiene): probability 0.005 is outside [0.02, 0.98]
with confidence='medium' and a reference-class spread of 0.30
```

Synthesis attempt 2 "fixed" it by moving the probability to exactly `0.02` while *lowering*
confidence to `low` and leaving the spread at 0.30. That passed — because
`check_calibration_hygiene` only evaluates confidence and spread when the probability is
**strictly outside** the band. Landing on the boundary skips the entire earned-extreme test.

That is not a calibration fix, it is a number moved to satisfy a grader. Two root causes:

1. **The gate reads a field the model controls.** `earned = f.confidence == "high" and spread <= 0.10`
   is a threshold on a self-reported label, not arithmetic. The synthesize agent has `tools=[]`
   and receives the frozen `OutsideView` unchanged on retry, so it *cannot* earn an extreme with
   new evidence — it can only relabel or retreat.
2. **`confidence` means nothing checkable.** The prompt defines it as evidence quality
   ("solid sample sizes, clear evidence"), but only one component (reference-class spread) is ever
   verified. The rest is asserted. `spec/superforecasting_methodology.md` never defines a
   confidence field at all.

**Intended outcome.** Confidence stops being a whole-forecast vibe and becomes a property of the
edge between a source and a claim: *for each claim, how strongly does each of its sources support
it?* Base-rate spread becomes a reported statistic. P16 stops being a gate and becomes
firm-but-bendable guidance the agent must argue against in writing.

Alongside that, the reasoning trail becomes legible: sources are clickable and verified against what
was actually fetched, base rates and adjustments say which decomposed sub-claim they answer, and
each methodology principle explains itself in one line instead of showing a bare `P7`.

## Decisions taken (from this session)

| Decision | Choice |
|---|---|
| ADR 13 conflict | Amend ADR 13 with an advisory carve-out; add new ADR for P16 |
| Purge scope | `Forecast.confidence`, `SubPrediction.confidence`, `forecast_updates.confidence`, and dead `ForecastRefreshResult.new_confidence`. **Keep `ResolutionCheckResult.confidence`** — different axis ("has this question resolved?"), not forecast quality |
| DB | Nothing deployed — edit DDL directly, recreate `superforecaster.db`. Alembic is future work, not this change |
| Source grading | Each claim carries a list of individually graded sources (`GradedSource`), each with its own confidence. Aggregation is two-level: max within a claim, weighted mean across claims |
| Class weighting | Record a per-class `weight`, and add a new pure check that `aggregate_base_rate` matches the weighted mean |
| Source URLs | `GradedSource.url` is optional and clickable, plus a new check that every cited URL appears in `deps.sources_seen`. Structured tool output (real `SourceRef` objects joined to claims) remains a separate spec |
| Sub-claim linkage | Mint `SubPrediction.id` in `run_decompose`; reference classes and adjustments carry `sub_claim_ids` |
| UI | Clickable links (new tab), state-backed collapsibles, sub-claim attribution, per-principle one-liners sourced from the methodology doc |

> **Open item to confirm during implementation:** the purge-scope answer included "Something else"
> with no text. I have read that as the dead `ForecastRefreshResult.new_confidence` field (no
> reader, no writer anywhere in the repo). If you meant something else, say so before I start.

---

## Part 1 — Purge `confidence`, add source confidence

### Delete (`backend/superforecaster/models.py`)

| Line | Field | Note |
|---|---|---|
| 100 | `Forecast.confidence` | Only behavioural consumer is the P16 gate being removed |
| 63 | `SubPrediction.confidence` | Required field that **no prompt sets** — `decompose.py` has zero confidence references |
| 270 | `ForecastRefreshResult.new_confidence` | Dead: no reader, no writer |
| 358 | `ForecastUpdateRecord.confidence` | Follows the DB column |
| 449 | `AddUpdateRequest.confidence` | API body for the DB column |

Keep `ResolutionCheckResult.confidence` (line 283) unchanged.

### Add

Confidence becomes a property of the **edge between a source and the claim it supports**, which
needs a per-source slot that does not exist today (`ReferenceClass.source` is one free-text string;
`Adjustment` has no source field at all).

```python
SourceConfidence = Literal["low", "medium", "high"]   # models.py:20, replaces Confidence

class GradedSource(BaseModel):
    source: str                      # "PitchBook 2019-2024"
    url: str | None = None           # when the agent has one; rendered as a link
    confidence: SourceConfidence     # how strongly THIS source supports THIS claim
    note: str                        # strength and relevance, in one line

class ReferenceClass(BaseModel):        # models.py:142
    ...
    sources: list[GradedSource] = Field(min_length=1)   # replaces `source: str`
    weight: float                                        # relative fit; see Part 2

class Adjustment(BaseModel):            # models.py:171
    ...
    evidence: str
    sources: list[GradedSource] = Field(default_factory=list)   # net-new
```

`min_length=1` on reference classes matches what the prompt already demands — *"A rate you reasoned
your way to is not a base rate."* Adjustments may legitimately have none (a judgment call with no
lookup), so the list is allowed to be empty and grades as `low`. That is the honest signal for a
narrative-driven move, and exactly the thing P16 exists to notice.

`ResolutionCheckResult` keeps its own literal (it is not a source grade).

### Aggregate — two levels

New pure functions in `checks.py`, beside the other pure helpers:

```python
def claim_support(sources: list[GradedSource]) -> SourceConfidence
def aggregate_source_confidence(outside, inside) -> SourceConfidence
```

**Level 1 — within a claim, take the strongest source (max), not the mean.** A claim backed by
PitchBook *and* a blog post is not worse-supported than one backed by PitchBook alone. A mean would
penalise citing extra corroboration, which trains the agent to hide sources — the opposite of what
this change is for. Empty list → `low`.

**Level 2 — across claims, take a weighted mean** of the level-1 results, floored to a tier:

- **Reference classes** weight by `weight` (fit), *not* `sample_size` — a large but ill-fitting
  class should count for less, which is what the prompt already asks the agent to do.
- **Adjustments** weight by `|magnitude|`, skipping `is_noise` items. This reuses the number
  `signed_adjustment` already treats as load-bearing; an adjustment contributing zero to the
  probability should not drag the grade.

Tier cutoffs go in `CheckThresholds` per ADR 14, not as literals.

The level-2 result is what surfaces as the forecast-level number — **derived, not asserted**, which
is the whole point. The model cannot flip it on retry without changing the underlying source grades.

### Downstream of replacing `ReferenceClass.source`

The scalar is read in two places that need updating with the field:
[inside_view.py:92](backend/superforecaster/agents/inside_view.py:92) (formats `rc.source` into the
prompt) and [runs.py:551](backend/superforecaster/runs.py:551) (emits `"source": rc.source` on the
`ref` SSE event — a spec3.1 wire-contract field).

### Prompts

- `outside_view.py` INSTRUCTIONS — add source grading to the `BUDGET` section, which already says
  *"If evidence is thin, say so in the class `source` and lower `sample_size`"*. Replace that
  smuggled signal with the explicit field.
- `inside_view.py` INSTRUCTIONS — same, for `Adjustment`.
- `synthesize.py:52-55` — delete the `Set confidence from the quality of the evidence` rubric.

---

## Part 2 — Record class weight, and verify the anchor

`implied_probability` ([checks.py:64-71](backend/superforecaster/checks.py:64)) anchors the entire
P6 chain on `o.aggregate_base_rate` — a single scalar. The outside-view prompt says:

> Set `aggregate_base_rate` to your best single anchor across the classes, **weighted by how well
> each fits this specific question.**

So the agent weights the classes and then discards the weights. `aggregate_base_rate` is a
black-box blend that nothing verifies: classes at 0.10 and 0.90 with an anchor of 0.85 passes today.

### Record the weight

```python
weight: float = Field(ge=0.0, le=1.0, description="Relative fit to this question")
```

Relative, not required to sum to 1 — normalise at computation time (an LLM handles "how well does
this fit" better than "make these sum to 1.0"). A `model_validator` on `OutsideView` rejects
all-zero weights, which would make the normalisation undefined.

### Verify the anchor

New pure check, registered in `FORECAST_CHECK_LABELS`:

```python
def check_aggregation(o, t) -> CheckViolation | None:
    """P7. The single anchor has to be what the classes actually say."""
    # aggregate_base_rate ≈ Σ(weightᵢ · base_rateᵢ) / Σweightᵢ, within CHECK_AGGREGATE_SLACK
```

Blocking, and unambiguously ADR 13 material — this is arithmetic that either holds or does not,
exactly the category ADR 13 was written to protect. It is a useful counterweight to demoting P16
in the same change: we are not weakening the checks layer, we are moving a heuristic out and
putting real arithmetic in.

Two implementation notes:

- **Principle number.** Filed as P7 (*"consult several reference classes… and aggregate them"*).
  `check_dragonfly` also occupies P7, which is fine — `check_decomposition` already spans P1+P2.
  P4 is the alternative if you would rather keep one check per principle slot.
- **It judges `OutsideView`, which `Synthesize` cannot change on retry.** So it joins the five
  existing checks that fire structurally no-op retries (see *Out of scope*). Blocking is still the
  right call — the violation travels out with the result and is visible — but it makes the
  blocking-set scoping more worth doing soon.

New threshold: `CHECK_AGGREGATE_SLACK` in `CheckThresholds`, default `0.05` to match
`derivation_slack`.

### Verify the citations

`GradedSource.url` makes citations clickable — which makes a fabricated URL render as an
authoritative link. `ForecastDeps.sources_seen` already records every URL actually retrieved (it
exists for leak auditing), so the honesty check is set membership:

```python
def check_citations(outside, inside, seen: list[SourceRef]) -> CheckViolation | None:
    """Every cited URL has to be one the agent actually fetched."""
```

Blocking, and the purest ADR 13 material in this change — a URL is either in the retrieved set or
it is not.

**Purity constraint.** `checks.py` forbids imports from `agents` and `graphs`, and `sources_seen`
lives on `ForecastDeps`. The check therefore takes `list[SourceRef]` as a plain argument
(`SourceRef` is in `models.py`, which `checks.py` already imports); the `Critique` node passes
`ctx.deps.sources_seen`. This widens the `run_forecast_checks` /
`run_forecast_checks_detailed` signatures by one parameter — every caller in tests and evals
needs the extra argument.

---

## Part 3 — Base-rate spread as a first-class statistic

**`_spread` is `max − min` (a range), not variance** — [checks.py:74](backend/superforecaster/checks.py:74).
The thresholds 0.20 / 0.10 are calibrated to a range; true variance across two classes 0.20 apart is
0.01. Keep the range math (it is the right statistic here) and name it honestly.

- Promote `_spread` → public `base_rate_spread()`, with a docstring saying it is public *because the
  UI needs it* — following the `signed_adjustment` precedent at [checks.py:49-58](backend/superforecaster/checks.py:49)
  and ADR 27's rule that chart and check must call the same helper so they cannot diverge.
- No change to `check_dragonfly` (P7) — it shares this helper and its threshold semantics must not move.
- Already present in `check_evidence` for both `dragonfly` and `calibration_hygiene`, and already
  rendered at [app.js:653](frontend/app.js:653) — so this is mostly a promotion and a rename, not new plumbing.

---

## Part 4 — P16 becomes advisory + guidance + a justification field

### The check stays, but changes shape

`check_calibration_hygiene` ([checks.py:281](backend/superforecaster/checks.py:281)) can no longer
read `confidence`. Re-scope it to the `disagreement` / `check_dragonfly` pattern — the agent writes
prose, the check verifies it exists:

```python
Forecast.extreme_justification: str = ""   # new field
```

- Fires with **`blocking=False`** when probability is outside the band and `extreme_justification`
  is empty or trivial.
- Also fires (advisory) when the probability sits at the tail of a wide base-rate spread — the case
  that motivated this change.

This keeps P16 falsifiable. Without a dedicated field, "write a strong justification" is
unverifiable prose inside `reasoning`, which is exactly the failure mode ADR 13 exists to prevent.

**This makes `blocking=False` its first production use.** The machinery is fully wired and tested
([test_graph_forecast.py:241](backend/tests/test_graph_forecast.py:241)) but has never been
exercised — every violation in the repo today uses the `True` default.

### Configurable guidance in the prompt

`synthesize.py:47-50` hardcodes `[0.02, 0.98]` in prompt text while the check reads
`th.calibration_floor` / `th.calibration_ceiling` from config — a live drift bug (setting
`CHECK_CALIBRATION_CEILING=0.999` makes the prompt lie, and
[test_checks.py:403](backend/tests/test_checks.py:403) proves that override is supported).

Fix it the way P6 already does: inject a computed block into the *user* prompt at runtime, alongside
`_arithmetic_block` and `_violation_block`. The agent is a module-level singleton, so per-run values
cannot live in `INSTRUCTIONS`.

```
CALIBRATION (principle 16)
Your band is [{floor}, {ceiling}]. Outside it, write `extreme_justification`:
which reference class carries the extreme, why the spread does not undercut it,
and what would have to be true for this to be wrong. If you cannot write that,
the number is telling you it is wrong — move it.
```

Firm, bendable, and the bend leaves an auditable trace.

---

## Part 5 — Tie every claim to the sub-claim it addresses

Today the three collections are independent lists with **no join anywhere** — not in `checks.py`,
not in `evals`, not on the wire. A user cannot tell which decomposed sub-claim a given base rate is
answering. The only existing link is one-directional and lossy: `run_outside_view` injects
researchable sub-claim *question strings* as a priority list
([outside_view.py:84-89](backend/superforecaster/agents/outside_view.py:84)) and gets no
machine-readable reference back.

### Give sub-claims an identity

`SubPrediction` has no `id`, `key`, or index. Mint one **deterministically in `run_decompose`**
after `result.output` (`sc1`…`sc5`) rather than asking the model for it — a model-supplied id can
duplicate or skip, and this needs to be a reliable key.

```python
class SubPrediction(BaseModel):
    id: str          # "sc1" — assigned in run_decompose, not by the model
    question: str
    ...
```

### Reference it

```python
class ReferenceClass(BaseModel):
    sub_claim_ids: list[str] = Field(default_factory=list)

class Adjustment(BaseModel):
    sub_claim_ids: list[str] = Field(default_factory=list)
```

A list, not a scalar: one class can inform several sub-claims, and an **empty list means "the
question as a whole"** — the case `run_outside_view` already handles when nothing is researchable.

New pure check (set membership, same family as `check_citations`): every referenced id exists in
the decomposition. Cheap, and it catches the model inventing `sc9`.

### Plumbing

- **`run_inside_view` does not receive the decomposition.** Its signature is
  `(input, outside, deps)` ([inside_view.py:85-89](backend/superforecaster/agents/inside_view.py:85)),
  and `AdjustInsideView` ([forecast.py:88-101](backend/superforecaster/graphs/forecast.py:88))
  has `ctx.state.decomposition` available but deliberately does not forward it. Both need widening.
- **`inside_view.py:91-94` renders reference classes as a lossy plain-text bullet list**
  (`name`, `base_rate`, `sample_size`, `source` only). New fields on `ReferenceClass` are invisible
  to the inside-view agent unless that f-string is updated too.
- Prompts in `outside_view.py` and `inside_view.py` must ask for the linkage explicitly.

### Two hazards to handle

- **`Forecast.decompositions` is regenerated by the synthesize agent** ("Carry the sub-claims into
  `decompositions`", [synthesize.py:172](backend/superforecaster/agents/synthesize.py:172)). Nothing
  enforces that it matches `Decomposition.sub_claims`. Once ids exist, add a check that the id sets
  are identical — otherwise every linkage silently dangles after synthesis.
- **`OutsideView` and `InsideView` are never persisted** ([db.py:128](backend/superforecaster/db.py:128)
  stores only `decompositions_json` and `research_json`). Linkage lives in-flight and on the SSE
  wire only. Persisting the views is out of scope here; worth noting so nobody expects a saved
  forecast to show it.

### On the wire

`sub`, `ref`, and `adj` events carry no index or parent pointer today
([runs.py:521-594](backend/superforecaster/runs.py:521)). Add `id` to `sub` and `sub_claim_ids` to
`ref` and `adj`. This also makes the existing `analog` convention explicit — analogs currently
belong to "the most recent `ref`" by undocumented arrival-order adjacency.

---

## Part 6 — Surface it in the UI

### Confidence chips

`CheckResult` has no `blocking` field, and the SSE `check` event
([runs.py:626](backend/superforecaster/runs.py:626)) does not carry one — so a passing advisory
check has nothing to key a warning affordance off. Add `blocking` to both.

Chips reuse the existing three-colour system at [index.html:155-165](frontend/index.html:155) —
`.chip.for` (green) / `.chip.warn` (yellow) / `.chip.against` (red), already themed for light and
dark. No new CSS. A `confidenceChip(v)` helper in `app.js` would be the first such helper; every
current call site inlines the ternary.

Chips render at both levels: one per `GradedSource`, and one per claim for the level-1 aggregate.
The `ref` SSE event ([runs.py:551](backend/superforecaster/runs.py:551)) carries `source` as a
scalar today and needs to carry the graded list instead.

Update `renderCheckEvidence`'s `calibration_hygiene` case ([app.js:696](frontend/app.js:696)) —
drop the `confidence` row, add spread-vs-band and the justification.

### (a) Clickable links, new tab

`h()` handles this unchanged — `href`, `target`, and `rel` all land via its `setAttribute` fallback
([app.js:36-61](frontend/app.js:36)). The `a` / `a:hover` styling already exists at
[index.html:76-77](frontend/index.html:76) using `--pv-focus`, which reads correctly on the cream
surfaces in both themes. **No new CSS.**

There are currently **zero anchors in `app.js`** — this introduces the first. Two places:

- **The `source` renderer** ([app.js:527-533](frontend/app.js:527)) already receives a real `p.url`
  and renders it only through `domainOf()`. Highest-value one-line fix in the whole plan.
- **`GradedSource.url`** on base-rate and adjustment sources, per Part 1.

Always `target="_blank"` with `rel="noopener noreferrer"` — neither attribute appears anywhere in
the repo today, so this sets the precedent.

While in the `source` renderer: `p.snippet` is a **dead branch** (`_source_payload` never emits it,
[observability.py:149-164](backend/superforecaster/observability.py:149)), and `p.domain`,
`p.tool`, `p.credibility` are emitted and ignored. Either wire them up or delete the dead branch —
leaving it is how the next reader wastes an hour.

### (b) Collapsible sections

Two existing patterns, and **the choice between them matters**:

| Pattern | Where | State survives re-render? |
|---|---|---|
| `state.collapsed[key]` + `scheduleRender()` | stage groups, [app.js:1043-1051](frontend/app.js:1043) | **Yes** |
| Native `<details open>` | `.ev.check`, [app.js:575-587](frontend/app.js:575) | **No** |

`render()` calls `root.replaceChildren` on every event ([app.js:710-725](frontend/app.js:710)), so
a native `<details>` a user opens mid-run snaps shut on the next event. This is a latent bug today
— unnoticed only because runs finish and stop re-rendering.

**Anything a user opens while a run is streaming uses the state-backed pattern.** Reuse
`state.collapsed` with namespaced keys (`src-<refIdx>`, `principle-<name>`). Note
`state.expandedBacklog` ([app.js:83](frontend/app.js:83)) is declared and never read — dead, worth
deleting while in here.

Body styling reuses `.work` ([index.html:237](frontend/index.html:237)) and `.evrow` — both are
generic, not scoped to `.ev.check`. Caveat: `.evrow .lbl` is `white-space:nowrap` with ellipsis, so
long sub-claim questions truncate; source lists want plain `.dim` rows instead.

### (c) Sub-claim attribution, client side

Backend work is Part 5. On the client, renderers receive **only `ev.payload`** — no `seq`, no index,
no group ([app.js:1052-1057](frontend/app.js:1052)) — so every linkage must arrive in the payload.
Client-side text matching is not an option: it would be the UI inventing a relationship the backend
never asserted, which ADR 27 exists to forbid.

- `sub` renderer ([app.js:535-542](frontend/app.js:535)) — render the new `id` as a base `.chip`.
- `ref` ([app.js:544-551](frontend/app.js:544)) and `adj` ([app.js:560-568](frontend/app.js:560)) —
  show which sub-claim each addresses, or "whole question" when the list is empty.

**Fix `analog` nesting at the same time.** `ReferenceClass.analogs` is a child collection in the
model, but analogs are emitted as *flat siblings* of `ref` ([runs.py:554-563](backend/superforecaster/runs.py:554))
and rendered as separate boxes. "Which parent does this belong to" is the identical problem
Part 5 solves — so analogs should nest under their `ref` in the same pass. This also makes base
rates the natural collapsible unit for (b).

### (d) Principle explanations

**Already on the wire.** The `check` event carries `principle` ([runs.py:628-640](backend/superforecaster/runs.py:628))
and the client discards it. Needs a text source, not a backend change.

Add a `PRINCIPLES` const next to `STAGE_META` ([app.js:22-28](frontend/app.js:22)), one line per
principle lifted from `spec/superforecasting_methodology.md`.

Two traps:

- **Key off `p.name`, not `p.principle`.** `check_decomposition` occupies the `"decomposition"`
  slot but reports `name: "knowability"` with `principle: 2` when its P2 arm fails
  ([checks.py:611](backend/superforecaster/checks.py:611)), so a failing check can show
  `principle: 2` under a `P1 · P2` label. The slot name is stable; the violation's principle is not.
- **A passing check renders nothing until opened** — the whole body is inside the collapsed
  `<details>`. If the principle line should be visible while collapsed it has to go in the
  `<summary>`, whose flex row already has `margin-left:auto` claimed by `.showwork`.

`STAGE_META.principles` (`"P4 · P7"`, rendered at [app.js:1048](frontend/app.js:1048)) is the second
surface that should get the same treatment — that is where a user looks first.

Do not duplicate the principle text between `app.js` and the methodology doc without a note saying
which is the source of truth. The doc is.

---

## Part 6 — Spec and ADR

- **`spec/ADR.md`** — amend ADR 13 with the carve-out (*a check may be advisory when its verdict is
  a judgment call rather than arithmetic*); ADR 13 stays in force for P1/P2/P6/P7/P9/P11/P12/P14/P15.
  Add the new ADR for P16, written in ADR 16's language: *an extreme probability is justified, not
  forbidden*. Supersede, do not delete. The amendment should note that the same change **adds**
  `check_aggregation` — the checks layer gains arithmetic as it sheds a heuristic, which is the
  distinction ADR 13 actually cares about.
- **Principle→enforcement table** in `spec/implemented/spec3.md:925-943` — row 16 currently claims
  `checks.check_calibration_hygiene`; P7 gains a second check.
- **`spec/superforecasting_methodology.md`** — define source confidence. It currently defines no
  confidence concept at all, which is why the field drifted.
- **`spec/CURRENT_STATE.md`** — models, checks, dependencies, env vars.
- **`spec/planned/spec4.md`** defines `process_score` as *"fraction of runs finishing with zero
  blocking violations — the primary metric."* Making P16 advisory changes that metric's meaning.
  Note it there.

---

## Files to change

**Backend:** `models.py`, `checks.py`, `config.py`, `db.py` (DDL + `save_forecast:265-276`),
`runs.py` (projections at 521-594, 608-609, 628-640, 719), `graphs/forecast.py` (`AdjustInsideView`
must forward `decomposition`), `graphs/update.py:138`, `__main__.py` (97, 111),
`api/forecasts.py:81`, `agents/{decompose,synthesize,outside_view,inside_view}.py`,
`observability.py:149-164` (dead `snippet`/`tool`/`credibility` keys), `fixtures/*.json`

**Frontend:** `app.js` — renderers at 527 (`source`), 535 (`sub`), 544 (`ref`), 553 (`analog`),
560 (`adj`), 575 (`check`), 696 (`calibration_hygiene` evidence); `STAGE_META` at 22;
`state.collapsed` at 81; saved-record shape at 467, 803, 1003. `index.html` only if a chip or
disclosure variant is genuinely missing — most of this reuses existing CSS.

**Tests:** `test_checks.py` (factory at 107-118 ripples through the file; P16 block at 373-407; the
`outside()` / `ref()` factories need the new `weight` + `sources` fields),
`test_db_forecasts.py:143` (raw SQL INSERT pins the DDL), `test_api_forecasts.py`,
`test_graph_update.py:216`, `test_graph_forecast.py`, `test_cron_orchestrators.py`,
`test_checks_detailed.py` (asserts `e["spread"]`, and needs a row for the new check)

---

## Verification

```bash
uv run pytest
```

1. **Unit — P16** — probability at the exact band boundary (`0.02` / `0.98`) is *not* a free pass;
   the attempt-2 scenario from this session (p=0.02, spread=0.30, no justification) now produces an
   advisory violation.
2. **Unit — aggregation** — `claim_support` returns the strongest source, so adding a `low` source
   to a `high` one does **not** downgrade the claim (the anti-hiding property); empty list → `low`;
   `aggregate_source_confidence` weights by `weight` / `|magnitude|` and skips noise.
3. **Unit — `check_aggregation`** — the anchor-outside-the-classes case that passes today
   (classes at 0.10 / 0.90, `aggregate_base_rate=0.85`) must now fail; an honestly weighted anchor
   must pass; all-zero weights rejected at the schema level; `CHECK_AGGREGATE_SLACK` override honoured.
4. **Graph** — a P16 violation no longer triggers a retry; `Synthesize` runs once.
   Existing `test_non_blocking_violation_does_not_retry` is the template.
5. **DB** — delete `superforecaster.db`, run `init_db()`, confirm a clean forecast save round-trips.
6. **Unit — linkage** — a `sub_claim_ids` entry naming a nonexistent id is a violation; ids survive
   the synthesize regeneration of `Forecast.decompositions`.
7. **End-to-end** — run a forecast through the frontend and confirm: source-confidence chips render
   in the right colours, P16 shows as a warning rather than a failure, the new P7 aggregation row
   appears in the critique list, and the base-rate spread reads correctly.
8. **End-to-end — the new UI**, all verified *mid-run* rather than after it finishes, since that is
   where the re-render bug lives: source links open in a new tab; a collapsible opened while
   streaming **stays open** across subsequent events; each base rate and adjustment shows which
   sub-claim it addresses; principle explanations expand on both the check rows and the stage
   headers.

## Suggested sequencing

This is large — six parts, three new checks, a model purge, and a UI pass. It splits cleanly into
four commits, each independently testable and reviewable:

1. **Purge + graded sources** (Part 1) — models, prompts, DB reset, aggregation helpers.
   Biggest blast radius; land it alone.
2. **New checks** (Part 2) — `check_aggregation`, `check_citations`, the `weight` field.
   Pure additions to `checks.py`; no UI.
3. **P16 advisory + spread promotion** (Parts 3-4) — the actual bug from the run that started this,
   plus the ADR amendment. Small once the groundwork exists.
4. **Linkage + UI** (Parts 5-6) — sub-claim ids, plumbing, and the four frontend changes.

Parts 1 and 2 both touch `ReferenceClass`; doing 1 first avoids editing the same model twice.
The ADR/spec updates (Part 6) travel with whichever commit makes each decision real.

## Deliberately out of scope

- **Structured/attributable sources.** Search tools return prose strings; `ResearchSummary.evidence`
  is `dict[str, list[str]]`; `SourceRef` (the only model with a URL) is never attached to a claim.
  Until that join exists, sources are agent-typed strings and "relevance to the claim" is asserted
  in `GradedSource.note` rather than computed. Worth its own spec — it would also fill the `credibility: None` hole
  deliberately left at [observability.py:149-164](backend/superforecaster/observability.py:149).
- **Alembic.** Agreed as future work.
- **The no-op retry problem.** Of the seven checks today, only P6 and P16 judge output `Synthesize`
  controls. The other five judge upstream artifacts the retry prompt explicitly declares
  *"unchanged and still stand"* — so they fire retries that cannot possibly fix them. After this
  change that is six of eight (P16 goes advisory, the new P7 aggregation check joins the upstream
  group), leaving **P6 as the only check whose retry does anything.** Real bug, separate change;
  flagging so the blocking set gets scoped deliberately rather than by omission.