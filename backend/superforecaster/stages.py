"""Per-stage forecast functions — the gated pipeline's unit of work.

Each function here is one stage step: at most a handful of agent calls, with the
code-stamped invariants the old graph nodes carried ported verbatim. The server's
state machine (`machine`) calls one function per user click; the CLI and evals call
`run_all`, which drives the same functions back-to-back with no gates.

The structural ordering guarantees survive as call signatures rather than prompts:
- `run_lenses_stage` never receives a rate, so choosing populations stays blind
  (pre-registration, ADR 40).
- `run_base_rate_step` re-stamps the lens identity from the *chosen* lens, so a
  research cell cannot re-weight its own population after seeing what it measured.
- `run_inside_step` requires a `ResearchedLens` — an adjust step literally cannot run
  before a measured rate exists (P4 as a signature, ADR 12).
- `run_synthesis_stage` runs reflect over every column's adjustments together
  (P14/P15 need cross-column visibility, ADR 31), then loops synthesize against the
  pure checks with one retry (`MAX_SYNTHESIS_ATTEMPTS`).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from .config import get_check_thresholds

from . import checks
from .agents.decompose import run_decompose
from .agents.inside_view import run_adjust_lens, whole_question_adjustments
from .agents.lenses import run_choose_lenses
from .agents.outside_view import (
    cell_deps,
    exhausted_notice,
    merge_base_rates,
    run_research_lens,
    whole_question_outside,
)
from .agents.reflect import run_reflect
from .agents.synthesize import run_synthesize
from .deps import ForecastDeps
from .models import (
    Adjustment,
    BaseRateStepPayload,
    CheckViolation,
    Decomposition,
    Forecast,
    ForecastInput,
    InsideStepPayload,
    InsideView,
    Lens,
    OutsideView,
    SourceRef,
    SubQuestionBaseRates,
    SubQuestionLenses,
    SubPrediction,
    SynthesisStepPayload,
)

STAGE_ORDER: tuple[str, ...] = (
    "decompose",
    "lenses",
    "base_rates",
    "inside_view",
    "synthesis",
)
"""The five gated stages, in the order a caller advances through them.

The order the functions below must run in, so it belongs beside them. It used to live
in the persistence module, which meant the state machine read the pipeline's shape back
out of the database layer that only stored it.
"""

MAX_SYNTHESIS_ATTEMPTS = 2

SYNTHESIS_FIXABLE = frozenset({"linkage", "derivation", "calibration_hygiene"})
"""The checks a second synthesis attempt can actually repair.

The synthesize agent controls the forecast's probability, reasoning,
extreme_justification, and carried-through decomposition ids — so `linkage`,
`derivation`, and `calibration_hygiene` are correctable. Everything else audits
evidence produced by *earlier* stages (a lens whose counted hits disagree with its
analogs, a one-sided inside view): no rewrite of the final forecast can fix those, and
retrying against them burned a full agent call per run before this set existed. Such a
violation still travels out with the result, visibly."""


async def run_decompose_stage(
    input: ForecastInput, deps: ForecastDeps
) -> Decomposition:
    """Principles 1 and 2 — Fermi-ize, and label what is researchable."""
    return await run_decompose(input, deps)


def normalize_weights(lenses: list[Lens]) -> list[Lens]:
    """Rescale a lens set to sum to 1.00 at two decimals, by largest remainder.

    Ratios are preserved, and every consumer computes Σ(w × v) / Σw — scale-invariant —
    so nothing derived changes. `weight` still means relevance only, never sample size
    (ADR 40). What changes is that the numbers a reader sees add up, and that an edited
    set has a defined shape to conform to.

    Rescaled rather than rejected: the type the agent returns is the type stored, so a
    strict validator on it would make the agent retry against arithmetic it has no reason
    to hit, spending search budget on a rounding rule. A hand-written set is rejected
    instead — see `SubQuestionLensesEdit`.

    Each share floors at 0.01, because `Lens.weight` is `gt=0.0`: a weight of exactly
    zero is not a legal lens, so a rounding rule must never produce one.
    """
    total = sum(lens.weight for lens in lenses)
    if total <= 0:
        return lenses

    floor = 1
    budget = 100 - floor * len(lenses)
    exact = [lens.weight / total * budget for lens in lenses]
    shares = [floor + int(x) for x in exact]

    # Largest remainder: hand the pennies rounding left over to the biggest fractions, so
    # the set sums to exactly 100 without any lens drifting more than one penny.
    remainder = 100 - sum(shares)
    order = sorted(
        range(len(lenses)), key=lambda i: exact[i] - int(exact[i]), reverse=True
    )
    for i in order[:remainder]:
        shares[i] += 1

    return [
        lens.model_copy(update={"weight": share / 100})
        for lens, share in zip(lenses, shares)
    ]


async def run_lenses_stage(
    input: ForecastInput,
    decomposition: Decomposition,
    sub_question: SubPrediction,
    deps: ForecastDeps,
) -> SubQuestionLenses:
    """Name populations for one sub-question. No tools, no rates — pre-registration."""
    chosen = await run_choose_lenses(input, decomposition, sub_question, deps)
    return chosen.model_copy(update={"lenses": normalize_weights(chosen.lenses)})


async def run_base_rate_step(
    input: ForecastInput,
    sub_question: SubPrediction,
    lens: Lens,
    deps: ForecastDeps,
) -> BaseRateStepPayload:
    """Measure exactly one population. Searches; budget-limited.

    The identity and weight come from the *chosen* lens, never from what came back — a
    research cell must not re-weight its own population after seeing what it measured.
    """
    cdeps = cell_deps(deps, sub_question.id or "")
    try:
        result = await run_research_lens(input, sub_question, lens, cdeps)
    except Exception as exc:
        if type(exc).__name__ == "UsageLimitExceeded":
            exhausted_notice(cdeps)
        raise
    researched = result.lens.model_copy(
        update={
            "name": lens.name,
            "population": lens.population,
            "why_it_fits": lens.why_it_fits,
            "weight": lens.weight,
            "weight_rationale": lens.weight_rationale,
            "sub_question_ids": [sub_question.id] if sub_question.id else [],
        }
    )
    return BaseRateStepPayload(
        lens=researched,
        disagreement=result.disagreement,
        sources=list(cdeps.sources_seen),
    )


async def run_inside_step(
    input: ForecastInput,
    sub_question: SubPrediction,
    payload: BaseRateStepPayload,
    deps: ForecastDeps,
) -> InsideStepPayload:
    """Move exactly one population's measured rate by its own modifiers.

    `lens_name` and `sub_question_ids` are stamped by code: a cell moved exactly one
    population, and a link it volunteered could point anywhere.
    """
    cdeps = cell_deps(deps, sub_question.id or "")
    try:
        result = await run_adjust_lens(
            input, sub_question, payload.lens, payload.disagreement, cdeps
        )
    except Exception as exc:
        if type(exc).__name__ == "UsageLimitExceeded":
            exhausted_notice(cdeps)
        raise
    adjustments = [
        a.model_copy(
            update={
                "lens_name": payload.lens.name,
                "sub_question_ids": [sub_question.id] if sub_question.id else [],
            }
        )
        for a in result.adjustments
    ]
    return InsideStepPayload(
        lens_name=payload.lens.name,
        adjustments=adjustments,
        steel_man=result.steel_man,
        sources=list(cdeps.sources_seen),
    )


def assemble_outside(
    decomposition: Decomposition,
    cells: list[tuple[SubPrediction, BaseRateStepPayload]],
) -> OutsideView:
    """Fold every measured population into one OutsideView. Pure."""
    return merge_base_rates(
        [sub_question for sub_question, _ in cells],
        [
            SubQuestionBaseRates(lens=p.lens, disagreement=p.disagreement)
            for _, p in cells
        ],
        decomposition,
    )


async def run_synthesis_stage(
    input: ForecastInput,
    decomposition: Decomposition,
    base_rate_cells: list[tuple[SubPrediction, BaseRateStepPayload]],
    inside_cells: list[tuple[SubPrediction, InsideStepPayload]],
    deps: ForecastDeps,
) -> SynthesisStepPayload:
    """The final stage: assemble both views, reflect, synthesize, critique.

    Arithmetic first — the anchor and the implied probability are computed by
    `checks`, not by the model — then the synthesis agent commits to a number that
    `check_derivation` holds within ±`derivation_slack` of implied (the configurable
    ±5-point rule). One retry on blocking violations, so a second attempt is a
    correction rather than a re-roll.
    """
    sources_seen: list[SourceRef] = []
    for _, p in base_rate_cells:
        sources_seen.extend(p.sources)
    for _, p in inside_cells:
        sources_seen.extend(p.sources)
    deps.sources_seen.extend(sources_seen)

    if base_rate_cells:
        outside = assemble_outside(decomposition, base_rate_cells)
    else:
        outside = await whole_question_outside(input, decomposition, deps, [])

    adjustments: list[Adjustment] = []
    steel_mans: dict[str, str] = {}
    for _, p in inside_cells:
        adjustments.extend(p.adjustments)
        if p.steel_man:
            steel_mans[p.lens_name or "?"] = p.steel_man
    if not adjustments:
        adjustments, steel_mans = await whole_question_adjustments(
            input, outside, deps, []
        )

    reflection = await run_reflect(
        input, decomposition, outside, adjustments, steel_mans, deps
    )
    inside = InsideView(
        adjustments=adjustments,
        steel_man=reflection.steel_man,
        what_would_change_my_mind=reflection.what_would_change_my_mind,
        bias_checks=reflection.bias_checks,
    )

    violations: list[CheckViolation] = []
    forecast: Forecast | None = None
    attempts = 0
    while attempts < MAX_SYNTHESIS_ATTEMPTS:
        attempts += 1
        forecast = await run_synthesize(
            input, decomposition, outside, inside, violations, deps
        )
        violations = checks.run_forecast_checks(
            forecast,
            decomposition,
            outside,
            inside,
            sources_seen=deps.sources_seen,
        )
        blocking = checks.blocking(violations)
        if not blocking:
            break
        if not any(v.name in SYNTHESIS_FIXABLE for v in blocking):
            # Every blocking violation indicts evidence from an earlier stage.
            # A retry would be told to fix something it cannot touch.
            break

    assert forecast is not None
    # Re-stamp the question metadata the caller supplied. The model has no business
    # restating it, and letting it try invites drift.
    forecast = forecast.model_copy(
        update={
            "question": input.question,
            "resolution_criteria": input.resolution_criteria,
            "resolution_date": input.resolution_date,
            "category": input.category,
        }
    )

    anchor, _rule = checks.anchor_from(outside, decomposition)
    implied = checks.implied_probability(outside, inside, decomposition)
    return SynthesisStepPayload(
        reflection=reflection,
        outside=outside,
        inside=inside,
        forecast=forecast,
        violations=violations,
        anchor=anchor if anchor is not None else outside.aggregate_base_rate,
        implied=implied,
        derivation_slack=get_check_thresholds().derivation_slack,
        attempts=attempts,
    )


async def run_all(
    input: ForecastInput,
    *,
    forecast_date: datetime | None = None,
    model: str | None = None,
    emit=None,
    store=None,
) -> tuple[Forecast, list[CheckViolation]]:
    """Run every stage back-to-back with no gates. The CLI and eval entry point.

    Same stage functions the server drives one click at a time; sequencing here is a
    for-loop and the per-stage fan-out is a plain `gather`, because gating is the
    database's job in server mode and nobody's job here.

    Returns the forecast plus any violations that survived the retry, so a caller can
    tell a clean forecast from one that never satisfied its own methodology.

    Pass `store` to keep the pages this run reads, so a later stage can read them back
    instead of searching again. Without one the run reads the web exactly as before.
    """
    deps = ForecastDeps(
        forecast_date=forecast_date,
        model=model,
        emit=emit,
        store=store,
    )

    decomposition = await run_decompose_stage(input, deps)
    researchable = [
        s for s in decomposition.sub_questions if s.knowability == "researchable"
    ]

    lens_groups = await asyncio.gather(
        *(run_lenses_stage(input, decomposition, s, deps) for s in researchable),
        return_exceptions=True,
    )
    cells: list[tuple[SubPrediction, Lens]] = []
    for sub_question, group in zip(researchable, lens_groups):
        if isinstance(group, BaseException):
            continue
        cells.extend((sub_question, lens) for lens in group.lenses)

    researched_results = await asyncio.gather(
        *(run_base_rate_step(input, s, lens, deps) for s, lens in cells),
        return_exceptions=True,
    )
    base_rate_cells: list[tuple[SubPrediction, BaseRateStepPayload]] = [
        (s, r)
        for (s, _), r in zip(cells, researched_results)
        if not isinstance(r, BaseException)
    ]

    inside_results = await asyncio.gather(
        *(run_inside_step(input, s, p, deps) for s, p in base_rate_cells),
        return_exceptions=True,
    )
    inside_cells: list[tuple[SubPrediction, InsideStepPayload]] = [
        (s, r)
        for (s, _), r in zip(base_rate_cells, inside_results)
        if not isinstance(r, BaseException)
    ]

    payload = await run_synthesis_stage(
        input, decomposition, base_rate_cells, inside_cells, deps
    )
    return payload.forecast, payload.violations
