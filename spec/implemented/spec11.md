# Spec 11 — Sub-questions that move together

**Status: implemented** (August 2026). ADR 65, amending ADR 33.

## Why

`checks.combine_sub_question_rates` multiplies sub-question rates as if they were
independent. Real sub-questions are not. "Anthropic has strong incentive to list" and
"Anthropic is operationally ready to list" move together, because company scale drives
both and wanting to list funds the readiness work. Multiplying them understates the answer.

Multiplying is not the neutral choice. **Two marginal probabilities do not determine their
joint.** They leave one free number, and `p₁ × p₂` picks one point in its range by
assumption. This spec names that assumption instead of hiding it.

**Outcome:** the anchor accounts for sub-questions that move together. A decomposition
that declares no groups produces the number it produces today, to the bit.

## The one free number

`P(A) = 0.75`, `P(B) = 0.60`. Let `x = P(A ∩ B)`. Every other cell is forced, because
`P(not A) = 1 − P(A)` needs no new information:

|  | B | not B | total |
|---|---|---|---|
| **A** | `x` | `0.75 − x` | 0.75 |
| **not A** | `0.60 − x` | `x − 0.35` | 0.25 |
| **total** | 0.60 | 0.40 | 1.00 |

Require all four cells `≥ 0` and `x` is trapped:

```
      0.35                0.45                    0.60
       |───────────────────|───────────────────────|
  lower bound          independent            upper bound
  max(0, p₁+p₂−1)      p₁ × p₂                min(p₁, p₂)

                           └──── w: 0 → 1 ────────┘
```

These are the **Fréchet-Hoeffding bounds**. Fixing the margins leaves exactly one degree of
freedom, and the marginals say nothing about where in that 25-point range the truth sits.
Today's code answers 0.45 and calls it arithmetic.

**`w` is the dependence parameter**: how far the joint sits from independence toward the
upper bound.

```
joint = independent + w × (upper bound − independent)
```

That is the **Fréchet family copula** `C = a·M + (1−a−b)·Π + b·W` with `b = 0` — a convex
mix of the upper-bound copula `M` and the independence copula `Π`.

### One construction, both chain rules

| rule | independent | bound | `w` moves the answer |
|---|---|---|---|
| conjunction | `math.prod(rates)` | `min(rates)` | up |
| disjunction | `1 − math.prod(1−p)` | `max(rates)` | down |

`math.prod` multiplies a list, the way `sum` adds one.

The upper bound is exact, not an approximation. `A ∩ B ⊆ A` and `A ∩ B ⊆ B`, so the overlap
can never exceed the smaller event. For a disjunction the union contains each event, so it
can never be smaller than the larger one — hence `max`, and `w` pushes *down*. Correlated
parts give a disjunction fewer distinct chances to fire.

The two arms are the same object. Apply the copula to the complements and use De Morgan,
and the disjunction formula falls out of the conjunction formula. They cannot drift apart.

### Where `w` comes from

The decompose agent names a **kind**. It never states a number.

```python
DEPENDENCE = {"none": 0.0, "shared_driver": 0.35, "one_causes_other": 0.50}
```

A model has no feel for the scale of `w`. A conditional moving from 0.60 to 0.72 sounds
modest and is 60% of the way to full dependence. A model does have a feel for "does one of
these cause the other".

This is the **beta-factor model** from nuclear probabilistic risk assessment: one
parameter, named cause classes, fitted from pooled outcome data.

**The three values are priors, not measurements.** Nothing supports 0.50 for a causal
link. Storing the kind is what makes them fittable later — every forecast that used
`shared_driver` contributes to one estimate. A per-forecast number never could.

## Assumptions

| # | Assumption | Enforced by |
|---|---|---|
| 1 | Groups partition — a sub-question is in at most one group | validator |
| 2 | Groups are independent of each other | step 2 uses `w = 0` |
| 3 | One `w` per group, with no pairwise structure inside it | the model shape |
| 4 | Only positive dependence. The 0.45 → 0.35 stretch is unreachable | `w ∈ [0, 1]` |
| 5 | `w` is linear distance to the bound | definition, chosen so it inverts |
| 6 | Kind → `w` is one table for every forecast | that is the point |

On assumption 3: three or more events have more than one free cell. `a·M + (1−a)·Π` is a
valid copula in any dimension, but it traces a one-parameter path through that larger
space rather than covering it.

On assumption 5: `w = (joint − independent) / (bound − independent)`. Inverting cleanly is
what lets a human check a kind against a stated conditional.

## The algorithm

```
combine(rates, rule, groups):
    1. each group -> reduce(its members, rule, DEPENDENCE[its kind])
    2. those values + every ungrouped rate -> reduce(..., rule, 0.0)
```

The same reducer twice. Worked, conjunction, `sq1=0.75 sq2=0.55 sq3=0.60 sq4=0.45`, with
`sq1` and `sq3` grouped:

| kind | `w` | group | chain |
|---|---|---|---|
| `none` | 0.00 | 0.450 | **11.1%** |
| `shared_driver` | 0.35 | 0.502 | **12.4%** |
| `one_causes_other` | 0.50 | 0.525 | **13.0%** |
| (upper bound) | 1.00 | 0.600 | **14.9%** |

A group of three coalesces as one block, not three pairs:

```
reduce([0.75, 0.60, 0.50], "conjunction", 0.50)
  = 0.225 + 0.50 × (0.500 − 0.225)
  = 0.3625
```

Group count is capped by arithmetic rather than by a rule. A group needs at least two
members and `sub_questions` holds 3 to 5, so a decomposition carries at most **two groups**.

## What changes, file by file

### `backend/superforecaster/models.py`

Next to `ChainRule`:

```python
DependenceKind = Literal["none", "shared_driver", "one_causes_other"]

DEPENDENCE: dict[str, float] = {
    "none": 0.0,
    "shared_driver": 0.35,
    "one_causes_other": 0.50,
}


class DependentGroup(BaseModel):
    name: str
    members: list[int] = Field(min_length=2)   # 1-based positions
    kind: DependenceKind = "none"
```

Members are positions, not `sq` ids. The decompose agent never sees ids — `with_ids`
stamps them after the agent returns — and an edit re-stamps them by position anyway, so
position is the stable thing.

On `Decomposition`, after `chain_rule`:

```python
    dependent_groups: list[DependentGroup] = Field(default_factory=list)
```

`default_factory=list` keeps every persisted checkpoint and fixture loading and producing
the number it produced before (ADR 28, the same reason `chain_rule` defaults to `custom`).
An empty list is `w = 0` everywhere, which is today's arithmetic.

One `model_validator(mode="after")`, three arms:

| arm | rejects |
|---|---|
| `custom` + groups | a chain rule with no formula for `w` to move |
| position outside `1..len(sub_questions)` | a group left behind by a deleted row |
| position in two groups | the double count that breaks the partition |

It raises rather than dropping. The decompose agent has `retries=1`, so a bad group returns
as a Pydantic retry with the message attached. A hand edit hits `model_validate` in
`machine.edit_payload` before `with_ids` runs, and `api/runs.py` already maps
`ValidationError` to a 422.

### `backend/superforecaster/checks.py`

```python
def _reduce(rates: list[float], rule: ChainRule, w: float) -> float
def combine_sub_question_rates(
    rates: list[float], rule: ChainRule, groups: list[DependentGroup] | None = None
) -> float | None
```

`combine_sub_question_rates` keeps `list[float]`. Groups key on position, so passing
`chain_inputs` rows would buy nothing and cost a public signature change.

Two call sites gain `d.dependent_groups` — `implied_probability` and `anchor_from`. That is
the whole behaviour change. Those two are the only readers, so the anchor and
`check_derivation` cannot disagree, which is the property ADR 33 exists to protect.

The `custom` arm is untouched. There is no chain, so there is nothing for `w` to move, and
the validator rejects the combination rather than ignoring it.

### `backend/superforecaster/agents/decompose.py`

A prompt block after `SAY HOW THEY COMBINE`. No code change; `with_ids` is untouched.

### `frontend/src/labels.js`

`DEPENDENCE_KINDS` — the label, the parameter, and a one-line meaning for each kind. The
payload stores only the kind, so a reader opening a saved run has to be told here what that
kind meant.

| kind | shown as | `w` | meaning |
|---|---|---|---|
| `none` | independent | 0.00 | Nothing links them. The rates multiply as they always did. |
| `shared_driver` | shared driver | 0.35 | One force moves both. Neither causes the other. |
| `one_causes_other` | one causes the other | 0.50 | One of them happening makes the other more likely. |

### `frontend/src/components/DependentGroups.jsx`

Read-only, shared by `RunView` and `SynthesisSection` so the two cannot describe the same
grouping differently. Per group: member chips, the kind as a chip with the parameter on
hover, the description of what the members share, and the meaning of the kind. Renders
nothing when nothing is grouped.

```
SUB-QUESTIONS THAT MOVE TOGETHER
[sq1] [sq3] [ONE CAUSES THE OTHER]
wanting to list funds the readiness work — One of them happening makes the other more likely.
```

### `frontend/src/components/DecomposeEditor.jsx`

A group is its own editable object; a sub-question row points at one through a `moves with`
select. Each group block carries an editable description, a kind select, the parameter, its
member chips, and a remove button.

A group is **not** identified by a name shared across rows. A name doing double duty as the
join key cannot be edited without unlinking its members, and it has to stay short enough to
fit a row control, which leaves no room to describe anything.

The shape makes two validator rules unreachable: a row holds one group index, so nothing
joins two groups, and a group under two members is dropped on save — with a warning on the
block first, so the drop is never a surprise. Removing a group unlinks its members and
shifts every later index down. `custom` hides the grouping controls entirely.

## Data lineage

```
run_decompose(input, deps) -> Decomposition
```

```json
{
  "sub_questions": [
    {"id": "sq1", "question": "Does Anthropic have strong incentive to list?", "probability": 0.75, "knowability": "judgment"},
    {"id": "sq2", "question": "Does the market window stay open?", "probability": 0.55, "knowability": "researchable"},
    {"id": "sq3", "question": "Is Anthropic operationally ready to list?", "probability": 0.60, "knowability": "researchable"},
    {"id": "sq4", "question": "Does the listing complete inside the window?", "probability": 0.45, "knowability": "researchable"}
  ],
  "chain_rule": "conjunction",
  "chain_note": "All four must hold for a listing to happen in the window.",
  "dependent_groups": [
    {"name": "wanting to list funds the readiness work", "members": [1, 3], "kind": "one_causes_other"}
  ]
}
```

```
checks.chain_inputs(d, o) -> rows                       [pure, no reads]
```

```json
[{"id": "sq1", "rate": 0.75, "source": "estimated"},
 {"id": "sq2", "rate": 0.55, "source": "researched"},
 {"id": "sq3", "rate": 0.60, "source": "researched"},
 {"id": "sq4", "rate": 0.45, "source": "researched"}]
```

```
checks.combine_sub_question_rates([0.75, 0.55, 0.60, 0.45], "conjunction", groups)
  -> _reduce([0.75, 0.60],        "conjunction", 0.50) = 0.525     members 1 and 3
  -> _reduce([0.525, 0.55, 0.45], "conjunction", 0.00) = 0.1299    the group + the rest
checks.anchor_from(o, d) -> (0.1299, "conjunction")
```

```
merge_base_rates(...) -> OutsideView                    [writes: run_steps.payload_json]
```

```json
{"aggregate_base_rate": 0.130, "lenses": ["..."]}
```

```
stages.run_synthesis_stage(...)                         [writes: run_steps, forecasts]
```

```json
{"anchor": 0.130, "implied": 0.152, "derivation_slack": 0.05, "attempts": 1}
```

The frontend reads `payload.anchor`. It does not re-derive the chain — `derive.js` stops at
`subQuestionRate`, so no JavaScript mirrors this arithmetic.

## No migration

`run_steps.payload_json` is `TEXT`, so a new field on `Decomposition` changes nothing in
SQLite and `SCHEMA_VERSION` does not move. `PUT /runs/{id}/steps/{id}/payload` takes a bare
dict and validates it as a `Decomposition`, so the field flows end to end the moment it
exists.

## Not in this spec

**The sensitivity sweep** — running `w` over `{0.0, 0.35, 0.7}` and stopping when the span
is small. That check earns its place when the next step costs a research call. Here the
agent names a kind for free, inside output it already produces, so there is no call to
skip.

**The two-direction check** — asking the agent `P(A | B)` and `P(B | A)`, converting each
to a `w`, and rejecting the pair when they disagree by more than about 0.2. This is the
strongest available guard against a wrong kind, and it costs two model calls per group. It
needs its own spec, and it is worth writing once there are resolved forecasts to score the
three defaults against.

**Anti-correlated sub-questions.** `w ∈ [0, 1]` cannot express them. Assumption 4.

## Sources

- Fréchet-Hoeffding bounds: Fréchet (1935), Hoeffding (1940). See Nelsen, *An Introduction
  to Copulas*.
- Fréchet family copula `a·M + (1−a−b)·Π + b·W`, and its multivariate form.
- Beta-factor model for common cause failure, used in nuclear probabilistic risk
  assessment.
- The values 0.35 and 0.50 have **no source**. They are chosen priors. Refit them against
  Brier scores once enough forecasts resolve.
