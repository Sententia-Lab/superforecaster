# Spec 4 — End-to-End Backtest and Golden Question Set

Score the forecasting graph against resolved questions with known outcomes, without
hindsight contaminating the result.

**Status: paused.** The design below is settled and the two contamination clamps it depends on
are already built (`superforecaster/tools.py`, `superforecaster/model_garden.py`). What is not
settled is the corpus — see *Why this is paused*. Resume once the question source is chosen.

---

## Why This Is Paused

The backtest needs resolved questions the agent can be scored against. Two sources were on the
table; neither is ready.

**The existing 66 questions cannot be clean-scored at all.** `backend/test_forecasting_baseline/run_baseline.py`
holds 66 resolved questions spanning **Sep 2020 → Sep 2024**. Every one of them predates the
training cutoff of every model Anthropic currently serves, so the model clamp can never find a
clean model for any of them:

```
clean coverage, 90d margin: 0/66
clean coverage,  0d margin: 0/66
```

Measured against the garden on 2026-08-03. Published training-data cutoffs:

| Model | Training cutoff |
|---|---|
| Opus 5 | May 2026 |
| Fable 5, Sonnet 5, Opus 4.8, Opus 4.7, Sonnet 4.6 | Jan 2026 |
| Opus 4.6, Opus 4.5 | Aug 2025 |
| Haiku 4.5, **Sonnet 4.5** | **Jul 2025** — the garden's floor |

A question needs `asked_at >= 2025-10-29` (Jul 2025 cutoff + the 90-day margin) to have any
clean model. The newest golden question is over a year too old.

*(Opus 4.1 had a Mar 2025 cutoff and would have reached back further, but it retires 2026-08-05
and was dropped from the garden rather than have a backtest silently change behaviour mid-week.)*

**A better source exists.** A larger corpus of resolved questions has been identified and is
being evaluated. Filling this spec in against a source that is about to be replaced would waste
the work — the harness shape below does not change, but the corpus determines whether clean mode
is usable at all.

---

## What Already Exists

Both clamps are built and tested. Nothing in this spec needs to re-derive them.

| Piece | Where | State |
|---|---|---|
| Clamp 1 — date-clamped tools | `superforecaster/tools.py` | Done. `_tavily_body`, `_wikipedia_params`, `_drop_leaked`, `SourceRef` audit trail |
| Clamp 2 — model garden | `superforecaster/model_garden.py`, `model_garden.json` | Done. `pick_clean_model`, `coverage`, `probe_all` |
| Threshold config | `config.CheckThresholds`, `MODEL_GARDEN_MARGIN_DAYS` | Done |
| Methodology checks | `superforecaster/checks.py` | Done — supplies `process_score` |
| Eval models | `superforecaster/models.py` | Done — `GoldenQuestion`, `QuestionScore`, `Scorecard` |

See `SPEC_IN_PROGRESS.md` → Backdating for how the clamps work.

---

## Requirements (when resumed)

### Golden question format

```
GoldenQuestion                    # one row of evals/golden_questions.json
  id                  str
  question            str
  resolution_criteria str
  asked_at            datetime    # both clamps key off this
  resolution_date     datetime
  outcome             float       # 0.0 or 1.0
  category            str
  baseline_prior      float       # human or crowd estimate — the number to beat
  contamination_risk  int         # 1 = obscure, 3 = certainly in training data
```

Whatever source is chosen must supply `asked_at` (when the question opened, not when it
resolved), a binary `outcome`, and ideally a crowd prior to score against.

### `evals/runner.py`

```
load_golden(path, *, tiers=None, limit=None) -> list[GoldenQuestion]
    Reads and filters the golden set.

run_one(q: GoldenQuestion, *, mode) -> QuestionScore                        # async
    In clean mode, calls pick_clean_model(q.asked_at) and returns a skipped
    QuestionScore when none exists — never falls back to a contaminated model.
    Otherwise runs forecast_graph with as_of=q.asked_at and model=<picked>,
    scores it, and records violations plus deps.sources_seen. Exceptions become
    QuestionScore.error so one bad question cannot abort the run.

run_backtest(questions, *, mode="clean", concurrency=4) -> Scorecard        # async
    Runs every question under a semaphore and aggregates.
```

### `evals/scoring.py`

```
brier(p: float, outcome: float) -> float
    (p - outcome) ** 2.

calibration_buckets(scores) -> list[CalibrationBucket]
    Deciles of predicted probability vs observed frequency. Mirrors the bucketing
    in db.calibration_report().

process_score(scores) -> float
    Fraction of runs finishing with zero blocking violations. Contamination-proof —
    this is the primary metric, and the only one that stays meaningful when clean
    coverage is low.

round_number_rate(scores) -> float
    P8. Fraction of forecasts landing on an exact multiple of 0.05. On a 2-decimal
    grid, 21 of 101 possible values are multiples of 0.05, so an unbiased forecaster
    sits near 0.21. A rate near 0.60 means the model is rounding to comfortable
    numbers. Flagged above CheckThresholds.round_number_rate.

build_scorecard(questions, scores) -> Scorecard
render_scorecard(s: Scorecard) -> str
```

### Two modes

| Mode | Model | Measures |
|---|---|---|
| `--clean` (default) | `pick_clean_model(q.asked_at)`, skip if none | the **methodology**, honestly. Older model, so worse absolute Brier |
| `--production` | `resolve_agent_model()` everywhere | the **shipped system**, contaminated wherever the question predates the model |

Running both is more informative than either alone: if production beats clean by a lot on tier 3
and barely at all on tier 1, that gap is a direct estimate of how much hindsight is doing the work.

### CLI

```bash
uv run python -m superforecaster test e2e                     # clean mode, the default
uv run python -m superforecaster test e2e --production
uv run python -m superforecaster test e2e --limit 10 --tier 1,2
uv run python -m superforecaster test e2e --audit             # print every leaked source
uv run python -m superforecaster test e2e --out reports/2026-08-03.json
```

Output shape:

```
mode=clean   n=120   88 scored   32 skipped (no clean model)   coverage 73%

              agent    baseline
mean Brier    0.171      0.203
process score 0.89                    <- fraction with zero blocking violations
round-number  0.24                    <- P8; ~0.21 is unbiased, >0.40 flags rounding
leaked sources 0                      <- must be 0; anything else is a tool-clamp bug

Models used
  anthropic:claude-sonnet-4-5-...  cutoff 2025-07-31   88 questions

Brier by contamination tier
  tier 1 (obscure)      0.166   n=31
  tier 2                0.174   n=39
  tier 3 (well-known)   0.172   n=18

Violations by principle
  P6  derivation          3
  P14 disconfirming       1
```

---

## Success Criteria

- `uv run python -m superforecaster test e2e --limit 3 --audit` completes and prints a scorecard.
- Every scored question reports a `model_cutoff` earlier than its `asked_at`.
- `leaked sources` is 0.
- Questions with no clean model are reported as skipped, never scored against a contaminated model.
- Clean coverage over the chosen corpus is reported in this file once measured.

---

## Open Questions

1. **Which corpus?** The identified source needs evaluating for: does it publish an open date
   (`asked_at`), a binary outcome, and a crowd prior? How many of its resolved questions opened
   after 2025-10-29 — the clean-eligible cutoff?
2. **What happens to the legacy 66?** Options: keep as production-mode-only with contamination
   tiers and label them clearly as not clean; or retire them once the new corpus lands.
   `backend/test_forecasting_baseline/run_baseline.py` raises on import today
   (`datetime.date(2022, 2, 1)` after `from datetime import datetime`) and has never run, so
   nothing depends on it.
3. **Forward-scored live set?** Open questions resolving after May 2026 would be past every
   current cutoff and give a fully uncontaminated signal — but produce no score until they
   resolve. Worth seeding now precisely because the payoff is delayed.

---

## Not In Scope Here

- **Component tests** (`superforecaster test component <agent>`) are specified in
  `SPEC_IN_PROGRESS.md` and are independent of this spec. They score individual agents against
  per-agent golden data and do not need the resolved-question corpus.
- **The two clamps** are already built. This spec consumes them; it does not modify them.
