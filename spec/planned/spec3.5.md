# Lenses: auditable base rates, per-lens modifiers, and a legible pipeline

## Context

PR #15 is merged (`main` @ `8bcbaac`). A real end-to-end run showed that most numbers on
screen are model assertions no check can verify, and that adjustments are applied at the
wrong level. This restructures the methodology so every number is either derived from
evidence or explicitly labelled a judgment — and so the frontend can explain the pipeline
rather than just display its output.

**What the numbers were.** `sample_size` and `analogs` were read by **no code at all**.
`base_rate` was asserted, not derived. `weight` was asserted. The `30%` on a decompose card
is a "rough working estimate" used only as a fallback. Only the blend was computed —
verified against the run: `(0.45×0.85 + 0.55×0.70) = 0.7675 → 77%`.

Source confidence is already display-only ([checks.py:988](backend/superforecaster/checks.py:988)) —
it never touches the probability, which is what you want. Unchanged.

---

## The structure

A **lens** is a reference population. It owns exactly one base rate, its own evidence, and
its own modifiers. There is no level at which a base rate is shared across lenses.

```
Question
└── 3–5 sub-questions                          (decompose; picks the chain rule)
    └── 1–3 lenses per sub-question            (P7 — different reference populations)
        ├── ONE base rate = Σ hits / Σ n       DERIVED from this lens's own evidence
        │   └── evidence: counted analogs + published statistics, pooled
        ├── 1–3 modifiers                      relative to THIS population
        ├── weight = relevance to this case    JUDGMENT — the only one left
        └── adjusted rate = base + Σ its own modifiers
    └── sub-question rate = Σ(weight × adjusted) / Σ weight     ← relevance only, never n
└── final = chain rule over sub-question rates
```

Worked, from your run:

```
sub-question  "Can it complete the regulatory prerequisites in 2026?"
├── lens A  all large-cap tech IPOs   base 85% (n=230)  M1 +5  M2 −2  → 88%   fit 0.45
└── lens B  AI labs specifically      base 70% (n=12)   M3 +4         → 74%   fit 0.55
→ sub-question = (0.45×0.88 + 0.55×0.74) = 80.3%
```

**Why modifiers hang off a lens.** A modifier is only meaningful relative to a population.
"Market cap exploded" is already baked into *large-cap tech IPOs* (no adjustment) but is the
whole differentiator against *all AI labs* (+5). Blending first and adjusting after
double-counts against populations that already control for the feature.

**Why the blend ignores `n`.** A lens with `n=12` can and should outweigh one with `n=20`
when it fits better. Sample size measures how well a population was *measured*, not how much
it *resembles this case* — and only the second is what a reference class is for. Thin lenses
keep full weight if they fit. The weight is the **one** number no check can verify, so
`weight_rationale` is mandatory and every lens's own adjusted rate is displayed.

---

## The graph — three flat fan-outs

Choosing lenses becomes its own step so populations are named **before** any rate is seen.
Otherwise the model can pick the population that yields the answer it likes; naming them
blind is pre-registration.

```
decompose
  → ⑂ choose_lenses (per sub-question) ⑃ → flatten to (sub-question, lens) pairs
  → ⑂ research_lens (per lens) ⑃        → merge_outside
  → ⑂ adjust_lens   (per lens) ⑃        → merge_inside
  → reflect → synthesize → critique ⟲
```

Three sequential flat maps, not nested forks — simpler, and the lens becomes the unit of
parallelism (up to 5 × 3 = 15 concurrent research cells). Each map needs the empty-list
bypass already used by `no_base_rate_cells`, since an empty `.map()` stalls the beta runner.

`STAGE_ORDER` becomes:

| # | stage key | label | principles |
|---|---|---|---|
| 1 | `decompose` | Decompose | P1, P2 |
| 2 | `lenses` | Choose lenses | P4, P7 |
| 3 | `outside` | Find base rates | P4 |
| 4 | `inside` | Adjust — inside view | P5, P9 |
| 5 | `reflect` | Reflect | P14, P15 |
| 6 | `synth` | Synthesize | P6, P8, P16 |
| 7 | `critique` | Critique | — |

The **adjusted rate is not a stage** — it is arithmetic over data already on screen, shown
inline on each lens card.

---

## Models

```python
class Evidence(BaseModel):
    """One block of cases behind a base rate. Counted or published, never both."""
    kind: Literal["counted", "published"]
    hits: int
    n: int
    note: str
    source: GradedSource | None      # required when kind == "published"

class Lens(BaseModel):
    """One reference population. Named before its rate is known."""
    name: str
    population: str                  # who is in it, precisely enough to count
    why_it_fits: str
    weight: float                    # relevance judgment
    weight_rationale: str            # required — the last unverifiable number

class ResearchedLens(Lens):
    evidence: list[Evidence] = Field(min_length=1)
    analogs: list[HistoricalAnalog]  # the named cases behind `counted` blocks
    # base_rate is a computed property: Σ hits / Σ n

class LensAdjustments(BaseModel):
    lens_name: str
    adjustments: list[Adjustment] = Field(min_length=1, max_length=3)
```

`sample_size` is deleted — it is `Σ n`, derived. `ReferenceClass.base_rate` stops being an
asserted field.

---

## Checks

```python
def lens_rate(lens) -> float                       # Σ hits / Σ n
def adjusted_lens_rate(lens, adjustments) -> float
def sub_claim_rate(sub_claim_id, o, i) -> float | None   # Σ(w × adjusted) / Σ w
def implied_probability(o, i, d) -> float          # chain rule over sub-question rates
```

- **New `check_base_rate_derivation`** — a `counted` block's `hits` must equal its analogs
  with `outcome == 1.0`, and its `n` must equal the analog count. A `published` block needs
  a source URL that was actually retrieved. This is what makes "7 of 10 did, so 70%" real.
- `check_dragonfly` compares **adjusted** lens rates, so a blend landing where no lens sits
  must be explained in `disagreement`.
- **Delete `synthesize._implied`** ([synthesize.py:96](backend/superforecaster/agents/synthesize.py:96)) —
  a hand-copied second implementation of the formula, which is exactly how the two drifted.
  Call `checks.implied_probability`.
- `check_derivation` uses the chain and becomes load-bearing again; today it compares a flat
  sum against a number that same flat sum suggested.

**The final probability will change on conjunctive questions.** That is the point.

## Prompts

- **decompose** — a sub-question whose outcome is already settled is not a forecast; it
  contributes `1.0` to a conjunction and wastes a research slot. Your S-1 case: prefer
  *"companies that filed and went public within the year"* over *"will they file"*.
- **choose_lenses** (new) — name 1–3 populations, define who is in each precisely enough to
  count, and set `weight` + `weight_rationale`. No rates yet.
- **research_lens** (new) — gather counted cases and published statistics for **one**
  population; do not state a rate, it is computed from what you gather.
- **inside_view** — delete the contradictory *"points on the FINAL probability"* paragraph
  ([inside_view.py:52](backend/superforecaster/agents/inside_view.py:52)). A magnitude moves
  **this lens's** rate. Add the question that makes per-lens adjustment work: *what does
  this population already control for?*

---

## Frontend

**Mobile overlap — a CSS source-order bug I introduced.**
[index.html:129](frontend/index.html:129) puts the `@media (max-width: 900px)` block
**before** the `.rail` rule it overrides at [index.html:134](frontend/index.html:134). Media
queries add no specificity, so source order wins and the *desktop* rule survives on mobile:
`position:sticky` and `max-height` both stay; only `border-right:none` wins, and only
because it carries `!important`. `.shell` collapses correctly because for it the media block
*is* the later rule — that asymmetry is the whole bug. The still-sticky rail slides over
`.main`, and since **neither sets a `background`**, you read both at once. Fix: move the
block after `.rail`, drop the `!important`, give `.rail` a background, drop
`max-height`/`overflow` on mobile.

**Markdown.** `h()` appends `document.createTextNode`
([app.js:241](frontend/app.js:241)), so everything is literal. No library, no build step —
add a small self-contained renderer. Agent prose is untrusted: escape first, then
`**bold**`, `*italic*`, `` `code` ``, blank-line paragraphs, `-`/`1.` lists, emitted through
the existing unused `html` hook ([app.js:235](frontend/app.js:235)). Prose fields only —
not `thought` deltas (markdown arrives split across chunks) and not `.collive` (one clipped
line; strip markers there).

**Cards follow the new structure.** Drop `N=`. A lens card shows its population, its
evidence as `7 of 10 counted · 140 of 230 published`, its derived rate, its modifiers, its
adjusted rate, and its weight with the rationale one click away. The sub-question card shows
the blend and what each lens alone implied.

---

## Files

| File | Change |
|---|---|
| `backend/superforecaster/models.py` | `Evidence`, `Lens`, `ResearchedLens`, `LensAdjustments`; drop `sample_size` |
| `backend/superforecaster/checks.py` | `lens_rate`, `adjusted_lens_rate`; rewrite `sub_claim_rate` / `implied_probability` / `check_derivation` / `check_dragonfly`; new `check_base_rate_derivation` |
| `backend/superforecaster/graphs/forecast.py` | three fan-outs; `lenses` stage; empty-map bypass per map |
| `backend/superforecaster/agents/` | new `lenses.py`; rework `outside_view.py`, `inside_view.py`, `decompose.py`; `synthesize.py` deletes `_implied` |
| `backend/tests/` | factories, derivation, fan-out |
| `frontend/index.html`, `frontend/app.js` | CSS order; markdown; stage list; lens cards |

---

## Verification

1. `cd backend && uv run pytest` — 374 baseline; factories and derivation tests change,
   anything beyond that is a regression.
2. **Base rate derived:** `counted 7/10` + `published 140/230` → `147/240 = 0.6125`; a class
   whose counted `hits` disagrees with its analogs must fail.
3. **Per-lens adjustment:** two lenses, different modifiers — each adjusts independently and
   the blend uses the *adjusted* rates.
4. **`n` never discounts a lens:** `n=12, fit=0.9` must outweigh `n=230, fit=0.3`. Assert no
   `n` term appears in the blend — the property most likely to get "helpfully" optimised in
   later.
5. **The chain:** two sub-questions under `conjunction` → `implied == r₁ × r₂`.
6. **Lenses are named before rates:** the `choose_lenses` step must complete with no
   evidence attached.
7. **Mobile:** 375px, scroll past the rail — it scrolls away, no overlay. 375/768/1440,
   light and dark.
8. **Markdown** renders bold and lists; `<img src=x onerror=alert(1)>` displays as text.
9. **A real run** — the number is expected to move; the point is whether it is better argued.

---

## Sequencing

The **frontend bug fixes (mobile, markdown) are independent** of all of the above and touch
nothing the methodology change touches. I'll land those first as their own commit so both
bugs are fixed immediately.

The methodology change is not independently shippable — models, checks, graph, and four
prompts must land together or the agents emit shapes the checks reject. It goes on its own
branch with a real run before merge.