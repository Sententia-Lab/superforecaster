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

from config import get_check_thresholds

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
    SubClaimBaseRates,
    SubClaimLenses,
    SubPrediction,
    SynthesisStepPayload,
)

MAX_SYNTHESIS_ATTEMPTS = 2


async def run_decompose_stage(
    input: ForecastInput, deps: ForecastDeps
) -> Decomposition:
    """Principles 1 and 2 — Fermi-ize, and label what is researchable."""
    return await run_decompose(input, deps)


async def run_lenses_stage(
    input: ForecastInput,
    decomposition: Decomposition,
    sub_claim: SubPrediction,
    deps: ForecastDeps,
) -> SubClaimLenses:
    """Name populations for one sub-question. No tools, no rates — pre-registration."""
    return await run_choose_lenses(input, decomposition, sub_claim, deps)


async def run_base_rate_step(
    input: ForecastInput,
    sub_claim: SubPrediction,
    lens: Lens,
    deps: ForecastDeps,
) -> BaseRateStepPayload:
    """Measure exactly one population. Searches; budget-limited.

    The identity and weight come from the *chosen* lens, never from what came back — a
    research cell must not re-weight its own population after seeing what it measured.
    """
    cdeps = cell_deps(deps, sub_claim.id or "", input.max_iterations)
    try:
        result = await run_research_lens(input, sub_claim, lens, cdeps)
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
            "sub_claim_ids": [sub_claim.id] if sub_claim.id else [],
        }
    )
    return BaseRateStepPayload(
        lens=researched,
        disagreement=result.disagreement,
        sources=list(cdeps.sources_seen),
    )


async def run_inside_step(
    input: ForecastInput,
    sub_claim: SubPrediction,
    payload: BaseRateStepPayload,
    deps: ForecastDeps,
) -> InsideStepPayload:
    """Move exactly one population's measured rate by its own modifiers.

    `lens_name` and `sub_claim_ids` are stamped by code: a cell moved exactly one
    population, and a link it volunteered could point anywhere.
    """
    cdeps = cell_deps(deps, sub_claim.id or "", input.max_iterations)
    try:
        result = await run_adjust_lens(
            input, sub_claim, payload.lens, payload.disagreement, cdeps
        )
    except Exception as exc:
        if type(exc).__name__ == "UsageLimitExceeded":
            exhausted_notice(cdeps)
        raise
    adjustments = [
        a.model_copy(
            update={
                "lens_name": payload.lens.name,
                "sub_claim_ids": [sub_claim.id] if sub_claim.id else [],
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
        [sub_claim for sub_claim, _ in cells],
        [
            SubClaimBaseRates(lens=p.lens, disagreement=p.disagreement)
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
        if not checks.blocking(violations):
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
    as_of: datetime | None = None,
    model: str | None = None,
    verbose: bool = False,
    emit=None,
) -> tuple[Forecast, list[CheckViolation]]:
    """Run every stage back-to-back with no gates. The CLI and eval entry point.

    Same stage functions the server drives one click at a time; sequencing here is a
    for-loop and the per-stage fan-out is a plain `gather`, because gating is the
    database's job in server mode and nobody's job here.

    Returns the forecast plus any violations that survived the retry, so a caller can
    tell a clean forecast from one that never satisfied its own methodology.
    """
    deps = ForecastDeps(as_of=as_of, model=model, verbose=verbose, emit=emit)

    decomposition = await run_decompose_stage(input, deps)
    researchable = [
        s for s in decomposition.sub_claims if s.knowability == "researchable"
    ]

    lens_groups = await asyncio.gather(
        *(run_lenses_stage(input, decomposition, s, deps) for s in researchable),
        return_exceptions=True,
    )
    cells: list[tuple[SubPrediction, Lens]] = []
    for sub_claim, group in zip(researchable, lens_groups):
        if isinstance(group, BaseException):
            continue
        cells.extend((sub_claim, lens) for lens in group.lenses)

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
