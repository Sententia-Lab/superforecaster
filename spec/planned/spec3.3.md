# Spec 3.3 — The Grid: A Column Per Sub-Question

**Status:** planned
**Supersedes:** `spec3.1.md` §3.3 (the event catalogue)
**Amends:** ADR 11, 12, 26, 27, 28, 29

---

## 1. The structure

Decompose fixes a **grid**. Rows are stages. Columns are sub-questions. Every research
row fans out one agent per column, then a barrier before the next row.

```
                        Will OpenAI go public in 2026?
                                    │
 Decompose  ┌───────────┬───────────┼───────────┬───────────┐   1 agent, N columns out
            │           │           │           │           │
           sc1         sc2         sc3         sc4
        public       docs        market      choose
       commitment   in time     appetite    exchange
            │           │           │           │
 ═══════════╧═══════════╧═══ await decompose ═══╧═══════════╧═══════════  BARRIER
            │           │           │           │
 Find Base  ▼           ▼           ▼           ▼               N agents, concurrent
   Rates  agent       agent       agent       agent            each: own budget,
            │           │           │           │              own card, own tool tail
            ▼           ▼           ▼           ▼
        1 sources   1 sources   1 sources   1 sources
        2 analogs   2 analogs   2 analogs   2 analogs
        3 rate %    3 rate %    3 rate %    3 rate %
        4 support   4 support   4 support   4 support
            │           │           │           │
 ═══════════╧═══════════╧═══ await base rates ══╧═══════════╧═══════════  BARRIER
            │           │           │           │
 Inside-    ▼           ▼           ▼           ▼               N agents, concurrent
   View   agent       agent       agent       agent            each seeded with ITS
            │           │           │           │              column's rate + classes
            ▼           ▼           ▼           ▼
        1 modifiers 1 modifiers 1 modifiers 1 modifiers
        2 sources   2 sources   2 sources   2 sources
            │           │           │           │
 ═══════════╧═══════════╧═══ await inside-view ═╧═══════════╧═══════════  BARRIER
            └───────────┴─────┬─────┴───────────┘
                              ▼
                        reflect (no tools)  ─ P14 steel-man, P15 five biases
                              │
 Synthesize                   ▼
                        one forecast
```

**The grid is fixed by decompose. Agents fill cells.** A column exists for every
sub-question at every row. A cell with no agent — a `judgment` sub-question has no base
rate to look up — renders a card saying why, rather than vanishing from the row.

### 1.1 Vocabulary

| Term | Means | In code |
|---|---|---|
| **stage** | a row | `decompose` / `outside` / `inside` / `synth` / `critique` |
| **column** | a sub-question, end to end | `sub_claim_id` — `sc1`…`scN` |
| **cell** | one column at one row | one `agent.run` + one card |
| **barrier** | await the whole row | `asyncio.gather` |

No new noun on the wire. Events carry the `sub_claim` they belong to; that is the entire
routing mechanism.

---

## 2. What this replaces

```
Decompose ──> Find Base Rates ──────────> Inside-View ────────> Synthesize
 sc1..sc4      ONE agent, 15 tool calls    ONE agent, 15 calls
               covers sc1..sc4             adjusts from ONE anchor
               (spends them all on sc1)
                    │
                    └─> aggregate_base_rate = Σ(w·rate)/Σw   ← mean across ALL classes
```

| # | Defect | Where | Fix |
|---|---|---|---|
| 1 | Anchor is a **mean** of conjunction factors — always ≥ their product | `checks.py:242` | `prod(rates)` per a typed `chain_rule` (§4) |
| 2 | Budget is a wall — `UsageLimitExceeded` kills the run | `config.py:159` | soft cline → converge; hard cap → degrade one cell (§7) |
| 3 | Nothing renders until a row ends | `runs.py:651` | cards open at `stage_started` (§3.3) |
| 4 | One budget shared across N sub-questions | `config.py:154` | budget is **per cell** (§7.3) |

`chain_note` already asks for the combination rule (`decompose.py:29`) and nothing reads
it. `checks.sub_claim_rate()` already computes the per-column rate and is display-only.

### 2.1 Worked example — why the mean is wrong

All four sub-questions must hold, so the rule is `conjunction`:

```
sc1 public commitment / S-1   0.55  researched
sc2 docs complete in time     0.70  researched
sc3 market appetite           0.60  researched
sc4 choose an exchange        0.80  judgment — SubPrediction.probability

mean    (0.55+0.70+0.60+0.80)/4  = 0.66     ← today's anchor: answers no question asked
product  0.55·0.70·0.60·0.80     = 0.185    ← what the decomposition actually says
```

The error has a direction. For a conjunction the mean of the factors is always ≥ their
product, so **every conjunctive question gets a systematically inflated anchor** — and the
entire P6 chain hangs off it.

---

## 3. Event protocol — superseding `spec3.1.md` §3.3

### 3.1 Envelope

```python
class RunEvent(BaseModel):
    seq: int
    run_id: str
    type: str
    stage: str = ""
    attempt: int = 1
    sub_claim: str | None = None   # NEW — "sc2", or None for whole-row work
    ts: datetime
    payload: dict[str, Any]
```

`sub_claim` sits on the **envelope**, not in `payload`, for two reasons: the client routes
on it before it looks at the payload, and `observability.py` — which builds the
`query`/`source`/`thought` payloads and must not know what a column is — would otherwise
have to inject it into three different payload shapes.

Defaulting to `None` keeps every buffered event, every localStorage trail written by the
current client, and every `RunSnapshot` deserializing unchanged.

### 3.2 Ordering under concurrency

`seq` remains a **total order** and the client's identity key: `Run._append` contains no
`await` and the loop is single-threaded, so interleaved cells still get strictly
increasing `seq`. `sub_claim` is purely the demux key.

The thought-flush rule changes shape:

```
today:  any emit          flushes THE buffer
after:  emit(sub_claim=X) flushes column X only
        stage boundary    flushes ALL columns — a barrier is a real barrier
```

Flushing every column on one column's event would splice sc2's half-written sentence in
front of sc1's tool call — the same garbage a shared buffer produces, rearranged.

### 3.3 Catalogue

Two new types. `claim` and `adj` gain a `sub_claim` tag. Everything else is unchanged.

| `type` | `sub_claim` | Emitted when | Payload |
|---|---|---|---|
| `stage` | — | node about to run | `{stage, attempt}` |
| **`column`** | **set** | **row start, one per sub-question, before any agent** | `{id, question, knowability, rationale, p, researching, anchor?, classes?}` |
| `brief` | — | Synthesize attempt 2 | `{anchor, implied, unchanged, violations, arithmetic, calibration, correction}` |
| `thought` | set in a cell | token deltas, coalesced 80ms **per column** | `{delta}` |
| `query` | set in a cell | tool call starts | `{tool, q, hits}` |
| `source` | set in a cell | tool call returns a new `SourceRef` | `{url, domain, title, query, published_date, tool, credibility}` |
| **`exhausted`** | **set** | **a cell blew `hard_depth`** | `{id, used, soft_depth, hard_depth, recovered}` |
| `sub` | — | Decompose | `{id, question, p, knowability, rationale}` |
| `note` | — | decompose / outside / inside | `{label, text}` |
| `claim` | **set** | Find Base Rates, at the barrier | `{id, question, knowability, rationale, rate, classes[]}` |
| `adj` | **set** | Inside-View, at the barrier | `{evidence, dir, mag, flip, noise, sub_claim_ids, support, sources}` |
| `bias` | — | reflect pass | `{bias, assessment}` |
| `draft` | — | Synthesize | `{p, ok, support, note}` |
| `check` | — | Critique | `{check, name, ok, principle, blocking, detail, evidence}` |
| `route` | — | Critique → Synthesize | `{text}` |
| `resume` | — | `POST /runs/{id}/resume` | `{from_node, completed_stages, max_iterations}` |
| `result` | — | End | `{forecast_id, question, probability, anchor, support, reasoning, waterfall, violations}` |
| `error` | — | exception | `{message, resumable, completed_stages, hint}` |
| `end` | — | always last | `{status, forecast_id}` |
| `truncated` | — | replay past an evicted buffer | `{dropped_before_seq, count}` |

**`column` payload**

```jsonc
{ "type": "column", "sub_claim": "sc2", "stage": "outside",
  "payload": {
    "id": "sc2", "question": "...", "knowability": "researchable",
    "rationale": "...", "p": 0.4,
    "researching": true,          // derived from knowability — not asserted by a model
    "anchor":  0.31,              // inside row only — checks.sub_claim_rate
    "classes": [ /* _class_payload */ ]   // inside row only — checks.classes_for
  } }
```

### 3.4 Corrections to `spec3.1.md` §3.3

That section has been stale since ADR 29 and the spec3.2 wire change, independent of this
work:

| §3.3 says | Reality |
|---|---|
| `ref`, `analog` events | replaced by `claim` (`runs.py:664`) |
| `sub.confidence`, `draft.confidence`, `result.confidence` | deleted by ADR 29 |
| `source.snippet` | never existed; `source.query` is new |
| — | missing `brief`, `resume`, `truncated`, `claim` |

`RunEvent`'s docstring repoints here. `spec3.1.md` §3.3 gains a superseded banner.

---

## 4. The anchor is the chain the decomposition describes

### 4.1 Model

```python
ChainRule = Literal["conjunction", "disjunction", "custom"]

class Decomposition(BaseModel):
    sub_claims: list[SubPrediction] = Field(min_length=3, max_length=5)
    chain_rule: ChainRule = "custom"   # default: old checkpoints still load (ADR 28)
    chain_note: str
```

`decompose.py` INSTRUCTIONS already say *"multiply for a conjunction, take the maximum for
alternatives, and say which it is."* This makes the distinction a field. `custom` is a last
resort and needs a stated reason in `chain_note`.

### 4.2 Combination

```python
# checks.py — public, for the same reason as signed_adjustment: the merge records the
# anchor with this and check_aggregation re-derives it with this, so the recorded number
# and the check cannot tell different stories.

def combine_sub_claim_rates(rates: list[float], rule: ChainRule) -> float | None:
    if not rates:               return None
    if rule == "conjunction":   return math.prod(rates)
    if rule == "disjunction":   return 1.0 - math.prod(1.0 - p for p in rates)
    return None                 # custom -> caller falls back to weighted_base_rate

def chain_inputs(d: Decomposition, o: OutsideView) -> list[dict]:
    """Every column, in order: {id, question, rate, source}.

    `source` is "researched" when a reference class named this column, "estimated"
    when none did and the decompose agent's own working probability stands in.
    """
```

**The empty-cell trap.** A product over only the *researched* columns silently treats the
rest as 1.0:

```
prod([0.55, 0.70, 0.60])         = 0.231   ✗  sc4 has no rate, so it vanishes
prod([0.55, 0.70, 0.60, 0.80])   = 0.185   ✓  sc4 contributes SubPrediction.probability
```

### 4.3 `check_aggregation`

```python
- def check_aggregation(o: OutsideView, t=None)                                 -> ...
+ def check_aggregation(o: OutsideView, d: Decomposition | None = None, t=None) -> ...
```

`d` is optional **and second** so `evals/components.py` and the existing tests keep working
without an edit.

```
d given, rule != custom  ->  implied = combine_sub_claim_rates(chain_inputs(d, o), rule)
otherwise                ->  implied = weighted_base_rate(o)      # the pre-3.3 arm
```

> **What this check now catches — stated plainly.** Once `run_outside_view` computes the
> anchor with the same function, no model performs that arithmetic, so the check can no
> longer catch a model doing it wrong in its head. It becomes a guard on the *artifact*:
> drift between the merge and the rule, a hand-built fixture, a checkpoint resumed from an
> older version.
>
> That is a **real weakening** of what ADR 29 added. The compensating gain is that the
> failure became structurally impossible rather than merely checked — the same move ADR 12
> made for "outside view first". Recorded here so it is not discovered later as a
> tautology nobody chose.
>
> **Rejected alternative:** add a `rate` field to each cell's output and check it against
> `sub_claim_rate`. That re-introduces a model-asserted number one function call away from
> the weights that imply it, for a check that fires only when the model's mental
> arithmetic is wrong about two or three numbers it can see.

### 4.4 P7 and P16 spread, measured within a column

`_spread(o)` is `max − min` across **all** classes. Once every class measures a different
column that number means nothing:

```
sc1 lens = 0.15 ─┐
sc4 lens = 0.80 ─┴─> 0.65 "disagreement"    ✗ two lenses on different questions
                                               P7 would fire on every run
```

```python
+ def sub_claim_spreads(o: OutsideView) -> dict[str | None, float]   # max-min per column
+ def worst_sub_claim_spread(o: OutsideView) -> float
```

- `check_dragonfly` fires when **any** column's spread exceeds the threshold, naming which.
- `check_calibration_hygiene` (P16) and `check_evidence` swap `base_rate_spread` →
  `worst_sub_claim_spread`. Post-fan-out the global spread is wide by construction, so the
  advisory would otherwise fire on nearly every run and become noise.
- `base_rate_spread` stays public — still correct when there is one group.

Grouping is read off `ReferenceClass.sub_claim_ids`, so every pre-3.3 fixture (all with
empty ids) forms one group and behaves identically.

### 4.5 `check_evidence` payload

| check | added |
|---|---|
| `dragonfly` | `spreads: {id: value}`; `spread` becomes the worst |
| `aggregation` | `rule`, `chain: [{id, question, rate, source}]` |
| `calibration_hygiene` | `spread` becomes the worst-within |

---

## 5. Find Base Rates — the fan-out

### 5.1 Seam

```python
# graphs/forecast.py — UNCHANGED
outside = await run_outside_view(ctx.state.input, ctx.state.decomposition, ctx.deps)
```

The fan-out lives **inside `run_outside_view`**, not in the graph node.

| Reason | Consequence |
|---|---|
| `test_checkpoints.py` ×9 and `test_graph_forecast.py` monkeypatch `fg.run_outside_view` | all stay green |
| ADR 11 names `run_<agent>` as *the* seam a test, an eval and a node all call | preserved |
| ADR 12: `graphs/` is methodology sequencing; a parallel map over N inputs is one row's internal shape | `forecast_graph.get_nodes()` unchanged; the `FindBaseRates → AdjustInsideView` edge and its P4 guarantee are untouched |

### 5.2 Per-cell output

```python
class SubClaimBaseRates(BaseModel):
    """One cell's answer — the four outputs the grid shows for one column."""
    reference_classes: list[ReferenceClass] = Field(min_length=2, max_length=3)
    disagreement: str = ""
    # no `rate`       -> checks.sub_claim_rate, computed from the class weights
    # no `confidence` -> checks.claim_support, derived from the strongest GradedSource
```

`min_length=2` **per column** is strictly stronger than the pre-3.3 whole-view
`min_length=2`, which "two lenses on sc1 and none on sc2–sc4" satisfied.

```python
# OutsideView
- reference_classes: Field(min_length=2, max_length=5)
+ reference_classes: Field(min_length=2, max_length=15)   # 5 columns x 3
```

### 5.3 Fan out, barrier, merge

```python
async def run_outside_view(input, decomposition, deps) -> OutsideView:   # signature unchanged
    researchable = [s for s in decomposition.sub_claims if s.knowability == "researchable"]
    if not researchable:
        return await _whole_question_cell(input, decomposition, deps)    # pre-3.3 path

    cell_depses = [_cell_deps(deps, s.id, input.max_iterations) for s in researchable]
    results = await asyncio.gather(                        # ◄── the barrier
        *(run_base_rate_cell(input, decomposition, s, d)
          for s, d in zip(researchable, cell_depses)),
        return_exceptions=True,        # one cell throwing must not cancel its row
    )
    for d in cell_depses:
        deps.sources_seen.extend(d.sources_seen)
    return _merge_base_rates(researchable, results, decomposition)
```

| | Rule |
|---|---|
| Cells that run an agent | `researchable` columns only. A `judgment` cell would return nothing (violating `min_length=2`) or invent a rate — which `check_decomposition`'s P2 arm exists to discourage |
| A `judgment` column | still gets a card (the grid is fixed by decompose) and still contributes `SubPrediction.probability` to the chain |
| Every cell fails | `run_outside_view` raises → ADR 28 checkpoint/resume, unchanged |
| `aggregate_base_rate` | computed: `combine_sub_claim_rates(chain_inputs(...), rule)` |
| `disagreement` | the non-empty per-column strings joined, each prefixed with its id — still the model's own words, so `check_dragonfly` has something to find |

```python
# _merge_base_rates — UNCONDITIONAL, not "if empty"
rc.model_copy(update={"sub_claim_ids": [sub_claim.id]})
```

A cell researched exactly one column; letting a model volunteer a different id would
re-open the linkage hole `check_linkage` closes. **A reference class belonging to no column
becomes structurally impossible**, and the prompt stops mentioning `sub_claim_ids`
entirely. `group_by_sub_claim`'s trailing "unattributed" group is deleted.

### 5.4 Cell prompt

Reuses today's `INSTRUCTIONS` with three deletions:

| Block | Why |
|---|---|
| AGGREGATE's `aggregate_base_rate` | a cell does not produce one. **`weight` stays** — it is what `sub_claim_rate` blends by |
| the `sub_claim_ids` instruction | stamped by code |
| the static BUDGET paragraph + `SEARCH BUDGET: at most N rounds` | §7 replaces both with something re-evaluated |

The user prompt gains the subject:

> Establish the outside view for ONE part of this question: `{id}` — {question}. …
> **The other parts are being researched separately; say nothing about them.**

That last clause is the point of the fan-out: today one agent gets 15 tool calls to cover
3–5 sub-questions and reliably spends them on the most searchable one.

---

## 6. Inside-View — the fan-out and the reflect pass

### 6.1 Per-cell output

```python
class SubClaimAdjustments(BaseModel):
    """One cell's answer — the two outputs the grid shows for one column."""
    adjustments: list[Adjustment] = Field(min_length=1, max_length=3)
    steel_man: str                      # P14, for THIS column
    what_would_change_my_mind: str
    # no bias_checks -> whole-question, see 6.2

# InsideView
- adjustments: Field(min_length=1, max_length=8)
+ adjustments: Field(min_length=1, max_length=15)
  bias_checks: Field(min_length=5, max_length=5)     # UNCHANGED
```

### 6.2 Why P14 and P15 need their own pass

```
4 columns x 5 bias_checks = 20   ->  InsideView wants exactly 5
```

| # | Reason |
|---|---|
| 1 | **No honest merge.** Concatenating four `confirmation` assessments produces text no reader wants; picking one discards three. Either way the artifact stops meaning what `check_bias_coverage` thinks it means |
| 2 | **Three of the five biases are only askable of the whole forecast.** *scope_insensitivity* ("would I give the same number for a 10× bigger version?") and *anchoring* are questions about the final probability. A column has none. Asking anyway produces five plausible paragraphs about nothing — precisely the failure P15 exists to catch |
| 3 | **`check_disconfirming` becomes checkable again.** It fails when *"every adjustment points the same direction"*. No single cell can evaluate that; the reflect pass sees all of them |
| 4 | **Cost is one request, not a search budget** — `tools=[]`, `get_synthesis_limits()` |

```python
# NEW backend/superforecaster/agents/reflect.py — the four-part module shape
class Reflection(BaseModel):
    """P14 + P15 over the whole question, after the per-column work."""
    steel_man: str
    what_would_change_my_mind: str
    bias_checks: list[BiasCheck] = Field(min_length=5, max_length=5)

INSTRUCTIONS = ...   # lifted VERBATIM from inside_view.py's SEEK DISCONFIRMATION and
                     # BIAS CHECK blocks — moved, not rewritten
def build_reflect_agent(model: str | None = None) -> Agent[ForecastDeps, Reflection]
def get_reflect_agent() -> Agent[ForecastDeps, Reflection]
async def run_reflect(input, decomposition, outside, adjustments, steel_mans, deps) -> Reflection
```

Per-column `steel_man` and `what_would_change_my_mind` are genuine per-sub-question content
and are what the card's second disclosure shows. Only the `InsideView` singletons and the
five biases come from the reflect pass.

### 6.3 `run_inside_view`

```python
async def run_inside_view(input, decomposition, outside, deps) -> InsideView:  # sig unchanged
    cells   = [s for s in decomposition.sub_claims if checks.classes_for(s.id, outside)]
    results = await asyncio.gather(*(...), return_exceptions=True)     # ◄── the barrier

    merged = [a.model_copy(update={"sub_claim_ids": [s.id]})
              for s, r in zip(cells, results) for a in r.adjustments]
    refl = await run_reflect(input, decomposition, outside, merged, steel_mans, deps)
    return InsideView(adjustments=merged,
                      steel_man=refl.steel_man,
                      what_would_change_my_mind=refl.what_would_change_my_mind,
                      bias_checks=refl.bias_checks)
```

Cell seed — the substantive methodology change on this row:

```
today:  BASE RATE TO ADJUST FROM: {outside.aggregate_base_rate}        ← GLOBAL
after:  BASE RATE FOR THIS SUB-QUESTION: {checks.sub_claim_rate(s.id, outside)}
        + checks.classes_for(s.id, outside) only
        + that column's disagreement
```

Cells that run an agent are columns with **at least one reference class**. No classes ⇒ no
base rate to adjust *from* ⇒ P5's premise ("the inside view modifies the outside view — it
does not replace it") does not hold ⇒ the card says so and no agent runs.

> **Known approximation.** `check_derivation` and `build_waterfall` add signed adjustments
> to `aggregate_base_rate` **flat**, while a modifier is now a delta from *its column's*
> rate. This spec deliberately does not change `implied_probability`, `check_derivation`,
> or `build_waterfall`: it is the load-bearing P6 check, two test suites pin it, and
> chain-propagated derivation is self-contained work with its own failure modes.
>
> The cell prompt compensates — magnitudes are points on the *final* probability arrived at
> via this sub-question, which is what the global-anchor prompt already assumes.
> **Follow-on:** propagate each column's adjusted rate back through the chain rule.

---

## 7. The search budget is a gradient, not a wall

### 7.1 Two channels

```
request 1        request 2        request 3        request 4 (validation retry)
    │                │                │                │
 [instr]──────────[instr]──────────[instr]──────────[instr]     (a) re-fetched per request
    └─ tool ─> [notice] ─ tool ─> [notice] ─ tool ─> [notice]   (b) at the decision point
                                                     ▲
                                            (b) silent here ────┘
```

| Channel | Why it is needed |
|---|---|
| **(b) tool-return notice** — primary | Arrives at the exact moment the decision is made. The model just asked for a result and will read it; the last line is the budget. Nothing to skim past. The only channel that can say *"this is your last tool result."* |
| **(a) `@agent.instructions`** | Covers (b)'s two holes: there is no tool result before request 1, and after the last one (b) is silent while the model may still make several requests. Instructions are re-fetched per model request, so pressure escalates within a run instead of being frozen into request 1 the way `SEARCH BUDGET: at most N rounds` is today |

### 7.2 The counter

```python
# deps.py
@dataclass
class SearchBudget:
    """One cell's search budget — and the column tag every event it produces carries."""
    sub_claim: str | None
    soft_depth: int        # the cline — converge from here
    hard_depth: int        # UsageLimits.tool_calls_limit — the wall
    used: int = 0
    exhausted: bool = False

    @property
    def past_the_cline(self) -> bool: ...

# ForecastDeps
+ budget: SearchBudget | None = None     # via replace(deps, budget=..., sources_seen=[])
```

- `used` is incremented **by the tools**, not read off `RunContext.usage`: a tool needs the
  count at return time and `usage.tool_calls` increments after. One counter, one owner.
- `find_disconfirming_evidence` runs three searches internally but is **one** tool call to
  pydantic-ai. Increment in the outer tool only, so `used` and `tool_calls_limit` count the
  same thing — and that thing matches the model's own sense of "a search round".
- **Rejected: a contextvar.** `override` and `capture_run_messages` already use contextvars;
  a third whose correctness rests on "`gather` wraps coroutines in Tasks, and Tasks copy
  the context" is three implicit couplings where a dataclass field is zero.

### 7.3 Config

```python
DEFAULT_CELL_SOFT_CALLS_PER_ITERATION = 1     # env CELL_SOFT_CALLS_PER_ITERATION
DEFAULT_CELL_HARD_HEADROOM            = 3     # env CELL_HARD_HEADROOM

def get_cell_budget(max_iterations: int) -> tuple[int, int]   # (soft, hard)
def get_cell_limits(max_iterations: int) -> UsageLimits       # tool_calls_limit = hard
```

The defaults are the decision, so the arithmetic belongs in the docstring:

| at `max_iterations=5` | tool calls | wall-clock |
|---|---|---|
| today | 15 total, pooled into the most searchable sub-question | serial |
| 3 researchable columns | 3 × 8 = 24 (1.6×) | ~⅓ |
| 4 researchable columns | 4 × 8 = **32** (2.1×) | ~¼ |
| 5 researchable columns | 5 × 8 = **40** (2.7×) | ~⅕ — the case to watch |

`get_research_limits` stays for the whole-question fallback and for evals.

### 7.4 Exceeding `hard_depth` — degrade one cell, never the run

`UsageLimitExceeded` is raised *before* the tools run, so catching it leaves no output.
Two mechanisms, shipped in this order:

**Step 1 — the sentinel.** ~10 lines, and it is what actually delivers the guarantee.

```
cell catches UsageLimitExceeded
  → budget.exhausted = True
  → emit("exhausted", {...}, sub_claim=id)
  → return None
_merge_base_rates contributes no classes for that column
chain_inputs falls back to SubPrediction.probability      ← §4.2's fix already covers this
run completes with the other columns intact
```

**Step 2 — the commit pass.** Recovers the partial research instead of discarding it.

```python
with capture_run_messages() as messages:
    try:
        return await run_agent(agent, prompt, deps=deps, usage_limits=limits, ...)
    except UsageLimitExceeded:
        deps.budget.exhausted = True
        history = _answer_dangling_tool_calls(messages)
# OUTSIDE the capture block — it records only the FIRST run in its context
with agent.override(tools=[], toolsets=[]):
    return await agent.run(COMMIT_NOW, message_history=history, deps=deps,
                           usage_limits=get_synthesis_limits())
```

> **The wrinkle.** The last `ModelResponse` holds `ToolCallPart`s that were never answered,
> and Anthropic rejects a history whose final assistant turn has unanswered `tool_use`
> blocks. Close each one with the budget notice — the same in-band channel, delivered at
> the only moment left. Truncating to the last `ModelRequest` also works but throws away
> the model's own last reasoning.

```python
def _answer_dangling_tool_calls(messages) -> list: ...
```

Concurrency is safe because `agent.override` is ContextVar-backed and each gathered cell is
a Task with a copied context — **provided `with with_model(...)` is entered inside the cell
coroutine**, not around the `gather`.

If step 2 proves brittle against a real provider, step 1 still holds the guarantee. That is
why they are separate steps.

---

## 8. Frontend — the grid on screen

Within ADR 24: vanilla, zero build, one `<style>` block, existing `--pv-*` tokens.

### 8.1 Routing

```js
run.stages.push({ key, stage, attempt, items: [], columns: null, columnOrder: [] });

case "column":     group.columns ||= {}; group.columns[id] = {...p, items: [], done: false};
                   group.columnOrder.push(id);
case "exhausted":  group.columns[id].exhausted = true;

// after the switch
ev.sub_claim != null -> group.columns[ev.sub_claim].items.push(...)  // placeholder if stray
                        merge consecutive `thought` WITHIN that column
                        `claim`/`adj` -> store on the column, done = true
ev.sub_claim == null -> group.items.push(...)                        // pre-3.3 path
```

`decompose`, `synth` and `critique` never see a `sub_claim` and take the existing path
byte-for-byte. `outside` and `inside` get column cards **plus** an untagged tail below them
(the anchor note, the two inside-view notes, the five bias events).

### 8.2 The card

```
.colcard[.busy]
├─ header   pct(rate ?? anchor) · sc2 · question · knowability chip · support chip
│           inside row only: "from {pct(anchor)}"
├─ .collive    tail of the last thought, else the last query — one clipped line, while !done
├─ ▸ N search steps    disclosure(`col-log-${group.key}-${id}`, …, dflt = !done)
│                      body carries data-scroll; renders via the existing EVENT_RENDERERS
├─ ▸ N modifiers       inside row only, dflt = false
└─ bottom   base-rate row: claim.classes.map(renderClass)  ← sources + analogs, unchanged
            exhausted:     .chip.warn "budget exhausted" + used/soft
```

Reuses `h()`, `disclosure()`, `renderClass()`, `renderSources()`, `supportChip()`,
`addresses()`, `pct()`. `runsOf()` is kept for the untagged rows; inside a card it is
superseded — a column's items are one homogeneous log plus a terminal finding, so there is
nothing alternating to fold.

### 8.3 Auto-open is a default, never a write

```js
- const disclosure = (key, summary, body) => {
-   const open = !!state.opened[key];
+ const disclosure = (key, summary, body, dflt = false) => {
+   const open = key in state.opened ? state.opened[key] : dflt;   // undefined until clicked
```

The tail passes `dflt: !column.done`, so it is open while the cell researches and closed
once its finding lands. `state.opened[key]` stays `undefined` until the user clicks; once
they click, their choice is sticky forever. Three lines, no new state, no cleanup pass, and
**no way to stomp a deliberate click** — which is why this is a default and not a write on
the `column` event.

### 8.4 Auto-scroll under full re-render

`render()` rebuilds the whole tree every frame, so a scrolled log loses its position.
`captureFocus`/`restoreFocus` handle focus only, and only for `data-focus`. Add the
symmetric pair over `[data-scroll]`:

```js
pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 8

no saved entry -> scrollTop = scrollHeight     // a new region starts pinned
pinned         -> scrollTop = scrollHeight     // follow the tail
else           -> scrollTop = saved.top        // reader scrolled up — do NOT yank back
```

### 8.5 Layout

```css
.cols { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:9px; }
```

Two or three cards across in the 900px main column, one at the existing `max-width:900px`
breakpoint. The diagram's four-across only fits a wide screen; grid reflow is the honest
answer. No new tokens, no new stylesheet.

### 8.6 Backlog card opens the editable form

Clicking a queued question opens the review form with its fields loaded and editable.
"Save changes" replaces the entry **in place** (queue order is the user's; an edit is not a
re-queue). "Run now" removes it — it is a run now, and leaving it queued invites a
double-spend of a slot. No re-critique: the critique is a one-pass P3 artifact tied to the
text the user originally typed.

---

## 9. What the grid asks for, and where it already lives

Nothing new is asked of a model. Every box maps to an existing typed field — the ADR 27
test.

### Find Base Rates, per column

| Grid | Model | Note |
|---|---|---|
| 1. list of sources | `ReferenceClass.sources: list[GradedSource]` | |
| 2. list of analogous base rates | `ReferenceClass` × N + `HistoricalAnalog` | ≥2 per column (P7) |
| 3. aggregated base-rate % | `checks.sub_claim_rate(id, o)` | **computed** from class `weight`s |
| 4. base-rate confidence (low/med/high) | `checks.claim_support(rc.sources)` | **derived** — see below |

> **Item 4 and ADR 29.** `Forecast.confidence` was deleted because a self-reported
> confidence is a second account that can disagree with the first. This requirement is met
> **derived rather than self-reported**: every `GradedSource` carries
> `confidence: high|medium|low` for *that source against that claim*, and
> `checks.claim_support()` grades a reference class by its **strongest** source, rendered by
> `supportChip()`. Grading by average would teach an agent to hide its weaker sources.
>
> **ADR 29 is not reversed.** No self-reported confidence field returns.

### Inside-View, per column

| Grid | Model |
|---|---|
| 1. list of modifiers | `Adjustment{evidence, direction, magnitude, flip_test, is_noise}` |
| 2. sources for each modifier (repeats allowed) | `Adjustment.sources: list[GradedSource]` — `_class_payload` builds a `url → ref` dict, so a repeat costs nothing |

The grid's two stacked inside-view boxes are one agent: research with tools, then commit to
the modifiers.

### Required by the code but absent from the grid

| | Why |
|---|---|
| A single anchor after the base-rate barrier | `Synthesize` and `check_derivation` consume `aggregate_base_rate`; the columns produce N rates. §4 combines them |
| The reflect pass after the inside-view barrier | `steel_man` and the five `bias_checks` cannot be asked of one column. §6.2 |

---

## 10. Fixed in passing

| | Where |
|---|---|
| `observability.py` diffs `deps.sources_seen` **by index** to detect new sources. A live bug even sequentially; under concurrency it mis-attributes and drops them. Fixed by giving each cell a private list and merging after the barrier | §5.3 |
| `evals/components.py` calls `run_inside_view(input, outside, deps)` — three arguments to a four-argument function. A `TypeError` that has never fired only because the eval corpus ships empty | §6.3 |
| `spec3.1.md` §3.3 has been stale since ADR 29 | §3.4 |
| `CURRENT_STATE.md`'s repo layout omits `spec3.2.md` | §11 |

---

## 11. ADRs

| # | Entry | Relationship |
|---|---|---|
| **30** | The grid: one column per sub-question, fanned out inside the node | **Amends ADR 12** without superseding — no node added, edges and ordering untouched, `get_nodes() == STAGE_KEYS`. Records why the seam is `run_<agent>`, and that stamped `sub_claim_ids` makes an unattributed class structurally impossible |
| **31** | P14 and P15 move to a reflect pass | **Amends ADR 11**: `inside_view → P5, P9`; `reflect → P14, P15`. Arguably more faithful to ADR 11's own rationale — `run_reflect` is testable against a fixed adjustment list with no network |
| **32** | The search budget is a gradient, and it is per cell | **Amends ADR 28**, whose budget-is-configuration rationale existed precisely because hitting the limit killed a run |
| **33** | The anchor is the chain the decomposition describes | **Amends ADR 29**. Records the weakening of `check_aggregation` and why P7's spread is now measured within a column |
| **29** | no change | Records that the grid's "base-rate confidence" was asked for and answered by the existing derived `claim_support`, so it is not re-litigated |
| **27** | extension in place | The `column` and `exhausted` events; `stage_started` widened to carry state, which is what lets cards render before research rather than after |
| **26** | note | Trails carry `columns`; trails written by the pre-3.3 client deserialize unchanged |

---

## 12. Verification

| Phase | Check |
|---|---|
| Envelope tagging | `uv run pytest` green. No visible change — every event carries `sub_claim: null` |
| Chain rule | `uv run pytest` green **with zero fixture edits** — the signal that the P7 rescoping was done by grouping rather than by rewriting |
| Fan-outs | One live run: N concurrent spans per row in logfire, each `query`/`source` in exactly one column, merged `sources_seen` with no duplicates and no gaps |
| Budget | `CELL_HARD_HEADROOM=0` forces the wall — the run still reaches `done` with the surviving columns' classes on the forecast |
| Frontend | Cards appear before any `query`; tails follow; scrolling up in one card while another streams does not yank it back; a manually collapsed card stays collapsed when its finding lands |

**Two canaries.** If either goes red the fan-out has escaped into `graphs/` and ADR 12 is
at risk:

| Assertion | Proves |
|---|---|
| `node_names == set(STAGE_KEYS)` | no graph node was added |
| stage start-before-finish | the fan-out stayed inside the node, so the barriers are still the graph's |
