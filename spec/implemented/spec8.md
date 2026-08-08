# Spec 8 — The readable run tree

**Status: implemented** (August 2026). ADRs 58–60. Depends on spec 10 (the sub-question rename).

## Why

The run tree renders every stage payload as flat rows. It works, and it does not explain
itself.

1. **Find lenses** puts a whole lens on one `evidence-row`. Name, population, and weight
   compete for one line.
2. **Base rates** flattens five kinds of text into one column: the lens population, the
   lens rationale, the counted fraction, the enumeration that produced the fraction, and
   the disagreement. A reader cannot tell which text came from the lens and which is
   net-new analysis. The enumeration runs to a full screen and buries everything under it.
3. **Modifiers** show a signed number beside a paragraph. Nothing names the modifier, so
   the list cannot be scanned.

Nothing collapses anywhere in the app. A finished run is one long scroll with the final
answer at the bottom — the last thing a reader reaches is the thing they came for.

**Outcome:** every section collapses, long prose sits behind an accordion, the origin of
each block of text is visible, and a finished run leads with its answer.

## Where it sits

```
RunView
├── 5 Synthesis          ← rendered first once complete
├── 1 Decompose          }
├── 2 Find lenses        }  each a collapsible StageSection
├── 3 Base rates         }
└── 4 Inside view        }
        └── card: Sub-question 1 — …
              ├── BaseRateCard "Base rate 1"   (was LensCard)
              └── BaseRateCard "Base rate 2"
```

## Decisions

| Question | Answer |
|---|---|
| Where does a modifier's title come from? | A new `Adjustment.title`, written by the agent |
| How does `sq1` become `Sub-question 1`? | Display-only, computed from the id at render time |
| What links the base-rate enumeration? | `Evidence.source` when present, else the cell's `sources` |
| How is prose rendered? | `react-markdown` + `remark-gfm`, everywhere |

## The one new dependency

`frontend/package.json` gains `react-markdown` and `remark-gfm`. The frontend has no
markdown renderer and no linkifier, and gfm's autolink rule turns a bare URL into an
anchor — so "every URL is a link" and "render markdown" are one change, not two.
`react-markdown` builds a React tree and never touches `dangerouslySetInnerHTML`, so
nothing here needs a sanitiser.

## Backend — one field

```python
class Adjustment(BaseModel):
    title: str = Field(
        default="",
        description="A short label for this move, six words or fewer. Names the "
        "mechanism, not the direction.",
    )
    evidence: str
    ...
```

`default=""` keeps every stored payload valid. The frontend falls back to the first
sentence of `evidence` when `title` is empty.

`agents/inside_view.py` gains one instruction block:

```
NAME EVERY ADJUSTMENT
Give each adjustment a `title` of six words or fewer. Name the mechanism, not the
direction. `evidence` carries the argument; `title` is how a reader finds it again.
```

## Data lineage — a modifier from agent to screen

```
POST /runs/{id}/steps/{step_id}/stream          stage = inside_view
  -> stages.run_inside_step(...) -> SubQuestionAdjustments
```

```json
{"lens_name": "FOMC rate-cutting cycle initiations in post-tightening environments",
 "adjustments": [
   {"title": "Already cutting subgroup has analogs",
    "evidence": "By Aug 7 2026 the cutting cycle that began in September 2024 would have run ~11 months …",
    "direction": "up", "magnitude": 0.10,
    "flip_test": "If the Fed had not started cutting, this would drop the estimate sharply",
    "is_noise": false, "sources": []}
 ],
 "steel_man": "The three cases where cuts were not underway by Aug 7 all resolved NO …"}
```

```
  -> InsideStepPayload(lens_name, adjustments, steel_man, sources)
                                                  [writes: run_steps.payload_json]
  -> SSE `run` frame -> machine.detail(run_id) -> RunStepOut.payload
  -> ModifierCard:  [+0.10]  Modifier 1 — Already cutting subgroup has analogs
                             By Aug 7 2026 the cutting cycle …
```

## Frontend — new files

| File | Contents |
|---|---|
| `components/Accordion.jsx` | `<Accordion summary={node} defaultOpen>` over native `<details>`/`<summary>`. No state, no animation library. |
| `components/Prose.jsx` | `<Prose>{text}</Prose>` — the one renderer for agent-written strings. `react-markdown` + `remark-gfm`, with `a` overridden to add `target="_blank" rel="noreferrer"`. |
| `labels.js` | `subQuestionLabel("sq1") -> "Sub-question 1"`; `ordinal("Lens", 1) -> "Lens 1"`. Both fall back to the raw input. |
| `components/LensOrigin.jsx` | The `FROM THE LENS` panel — lens name, population, weight chip. Tinted surface with a left accent border, so lens output reads differently from net-new analysis. |
| `components/BaseRateCard.jsx` | One base-rate cell. |
| `components/ModifierCard.jsx` | One inside-view cell. |

`LensCard.jsx` is deleted. Its two branches had diverged past the point where one component
serving both made sense; `SupportChip` and `adjustedRate` move into the two new cards.

## The base-rate card

```
┌ Accordion, default open ───────────────────────────────────────────┐
│ Base rate 1   FOMC rate-cutting cycle…   [w 0.50]  72.7%  [high]   │
├────────────────────────────────────────────────────────────────────┤
│ ┌ FROM THE LENS ───────────────────────────────────────────┐       │
│ │ FOMC rate-cutting cycle initiations in post-tightening…  │       │
│ │ All calendar years from 1970 to 2025 in which the Fed…   │       │
│ └──────────────────────────────────────────────────────────┘       │
│                                                                    │
│ Counted rate: 72.7% from 8 of 11 counted                           │
│ ▸ 8/11 [counted]  How this was counted     → note + source links   │
│ ▸ Disagreement                             → disagreement          │
└────────────────────────────────────────────────────────────────────┘
```

- `lens.why_it_fits` is **removed** from this card. It restates the sub-question, and it
  now appears in section 2 where it belongs.
- `weight` moves into a chip, matching the lens cards.
- `Evidence.note` moves behind `How this was counted`, one accordion per evidence block.
  The `hits/n` fraction and `kind` chip stay in the summary.
- Sources inside that accordion: `Evidence.source.url` when present; otherwise the cell's
  `BaseRateStepPayload.sources` as linked chips; otherwise `no source retrieved`.

## The modifier card

```
┌ Accordion, default open ───────────────────────────────────────────┐
│ Modifiers   FOMC rate-cutting cycle…            72.7% → 78.7%      │
├────────────────────────────────────────────────────────────────────┤
│ ┌ FROM THE LENS · name · population ───────────────────────┐       │
│                                                                    │
│ [+0.10]  Modifier 1 — Already cutting subgroup has analogs         │
│          By Aug 7 2026 the cutting cycle that began…               │
│ [−0.04]  Modifier 2 — Cutting cycle may have paused                │
│          The cutting cycle that started in Sep 2024…               │
│ [±0.00]  Modifier 3 — Tariff inflation differentiates 2026 [noise] │
│                                                                    │
│ 72.7% + 0.10 − 0.04 → 78.7%                                        │
│ ▸ Steel man                                                        │
└────────────────────────────────────────────────────────────────────┘
```

The magnitude reuses `.adj-mag` and its `up` / `down` / `noise` tones, chip-shaped and
moved into the title row. The arithmetic line is unchanged.

## The synthesis table

Today it is one row per lens, keyed by a bare id. It becomes a grouped table that lays out
the whole analysis: a full-width header row per sub-question carrying the question itself,
then its lenses, then every modifier under its lens.

A wide `Sub-question` column is the wrong shape — the question is a sentence and
`.main-inner` is 880px. A spanning header row keeps the numeric columns narrow.

```
┌─────────────────────────────────────────────────────────────────────┐
│ Sub-question 1 — Will the Fed have begun cutting rates by Aug 2026? │
├────────────────────┬──────────────────────┬───────┬───────┬───┬─────┤
│ Lens               │ Modifier             │  Move │Counted│Adj│  w  │
│ Lens 1 — FOMC…     │                      │       │ 72.7% │78.7│0.50│
│                    │ Modifier 1 — Already…│ +0.10 │       │   │     │
│                    │ Modifier 2 — Cutting…│ −0.04 │       │   │     │
│ Lens 2 — Post-hik… │                      │       │ 40.0% │45.0│0.50│
├────────────────────┴──────────────────────┴───────┴───────┴───┴─────┤
│ Blended                                                      61.9%  │
└─────────────────────────────────────────────────────────────────────┘
```

Modifier rows come from `derive.adjustmentsForLens(lens, inside)` — already exported, and
`SynthesisSection` never called it. `Blended` moves out of a per-lens cell into its own
footer row per sub-question, which is what the number actually describes. The table gets an
`overflow-x: auto` wrapper.

## Ordering and collapse

```js
const done = synthesisStep?.status === "complete";
```

- Sections 1–4 default open while a run is in flight, collapsed once it is done.
- Section 5 renders **above** section 1 when `done`. The `n` badges keep their numbers, so
  the pipeline order stays readable.
- The `Run section` button lives in the summary row and calls `e.stopPropagation()`, so
  clicking it does not toggle the section.

## theme.css

New: `details.acc` / `.acc > summary` / `.acc-body` (chevron rotates on `[open]`),
`.lens-origin`, `.mod-title`, `.adj-mag.chip`, `.arith-scroll`, `.arith tr.group`,
`.arith tr.mod`, `.arith tr.total`, and element styles for what react-markdown emits.

**`.prose` loses `white-space: pre-wrap`.** Markdown handles line breaks now; keeping both
double-spaces every paragraph.

`.chip`, `.card`, `.card.nested`, `.card-sub`, `.rate`, `.src-chip`, `.evidence-row` and
`.adj-mag` are reused unchanged.

## Tests

An `Adjustment` with no `title` validates, and one with a title round-trips through
`model_dump_json`. The frontend has no test runner; CI runs `npm run build`.
