"""Per-stage forecast functions — the gated pipeline's unit of work."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Awaitable, Callable, TypeVar

from pydantic_ai.exceptions import UsageLimitExceeded

from . import checks
from .agents.decompose import run_decompose
from .agents.inside_view import run_adjust_lens
from .agents.lenses import run_choose_lenses
from .agents.outside_view import run_research_lens
from .agents.reflect import run_reflect
from .agents.synthesize import run_synthesize
from .config import get_check_thresholds
from .deps import ForecastDeps
from .events import Exhausted
from .models import (
    BaseRateStepPayload,
    CheckViolation,
    Decomposition,
    Forecast,
    ForecastInput,
    InsideStepPayload,
    InsideView,
    Lens,
    OutsideView,
    ResearchedLens,
    SubPrediction,
    SubQuestionLenses,
    SynthesisStepPayload,
)

STAGE_ORDER: tuple[str, ...] = (
    "decompose",
    "lenses",
    "base_rates",
    "inside_view",
    "synthesis",
)

MAX_SYNTHESIS_ATTEMPTS = 2

SYNTHESIS_FIXABLE = frozenset({"derivation", "calibration_hygiene"})
"""The checks a second synthesis attempt can repair. Everything else audits evidence
from an earlier stage, and a retry would be told to fix something it cannot touch."""


async def run_decompose_stage(
    input: ForecastInput, deps: ForecastDeps
) -> Decomposition:
    """Principles 1 and 2. The seam `app.machine` and the tests call."""
    return await run_decompose(input, deps)


def normalize_weights(lenses: list[Lens]) -> list[Lens]:
    """Rescale a lens set to sum to 1.00 at two decimals, by largest remainder (ADR 54).
    Each share floors at 0.01 because `Lens.weight` is `gt=0`."""
    total = sum(lens.weight for lens in lenses)
    if total <= 0:
        return lenses
    floor = 1
    budget = 100 - floor * len(lenses)
    exact = [lens.weight / total * budget for lens in lenses]
    shares = [floor + int(x) for x in exact]
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
    """Name populations for one sub-question. No rates yet — pre-registration."""
    chosen = await run_choose_lenses(input, decomposition, sub_question, deps)
    return chosen.model_copy(update={"lenses": normalize_weights(chosen.lenses)})


def _cell_deps(deps: ForecastDeps, sub_question: SubPrediction) -> ForecastDeps:
    """A deps copy for one cell, with its own `sources_seen` (see `runner`)."""
    return replace(deps, sub_question=sub_question.id, sources_seen=[])


def _notice_exhausted(deps: ForecastDeps) -> None:
    if deps.emit is not None:
        deps.emit(Exhausted(), deps.sub_question)


async def run_base_rate_step(
    input: ForecastInput,
    sub_question: SubPrediction,
    lens: Lens,
    deps: ForecastDeps,
) -> BaseRateStepPayload:
    """Measure one population. The lens identity and weight come from the chosen lens,
    so a cell cannot re-weight its population after seeing what it measured."""
    cdeps = _cell_deps(deps, sub_question)
    try:
        result = await run_research_lens(input, sub_question, lens, cdeps)
    except UsageLimitExceeded:
        _notice_exhausted(cdeps)
        raise
    researched = ResearchedLens(
        **lens.model_dump(),
        evidence=result.evidence,
        analogs=result.analogs,
        sub_question_ids=[sub_question.id] if sub_question.id else [],
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
    """Move one measured lens by its own modifiers. `lens_name` and `sub_question_ids`
    are stamped by code."""
    cdeps = _cell_deps(deps, sub_question)
    try:
        result = await run_adjust_lens(
            input, sub_question, payload.lens, payload.disagreement, cdeps
        )
    except UsageLimitExceeded:
        _notice_exhausted(cdeps)
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
    """Every measured lens in one view, with the anchor `checks.anchor_from` computes."""
    notes = [
        f"{sq.id}: {p.disagreement.strip()}"
        for sq, p in cells
        if p.disagreement.strip()
    ]
    view = OutsideView(
        lenses=[p.lens for _, p in cells],
        aggregate_base_rate=0.0,
        disagreement=" · ".join(notes),
    )
    anchor, _rule = checks.anchor_from(view, decomposition)
    view.aggregate_base_rate = checks.clamp(anchor if anchor is not None else 0.0)
    return view


async def run_synthesis_stage(
    input: ForecastInput,
    decomposition: Decomposition,
    base_rate_cells: list[tuple[SubPrediction, BaseRateStepPayload]],
    inside_cells: list[tuple[SubPrediction, InsideStepPayload]],
    deps: ForecastDeps,
) -> SynthesisStepPayload:
    """Assemble both views, reflect, then synthesize against the checks with one retry."""
    if not base_rate_cells:
        raise RuntimeError("no researchable sub-question produced a base rate")
    if not inside_cells:
        raise RuntimeError("no lens produced an adjustment")

    for _, p in (*base_rate_cells, *inside_cells):
        deps.sources_seen.extend(p.sources)

    outside = assemble_outside(decomposition, base_rate_cells)
    adjustments = [a for _, p in inside_cells for a in p.adjustments]
    steel_mans = {p.lens_name: p.steel_man for _, p in inside_cells if p.steel_man}
    reflection = await run_reflect(
        input, decomposition, outside, adjustments, steel_mans, deps
    )
    inside = InsideView(**reflection.model_dump(), adjustments=adjustments)

    violations: list[CheckViolation] = []
    attempts = 0
    while True:
        attempts += 1
        answer = await run_synthesize(
            input, decomposition, outside, inside, violations, deps
        )
        forecast = Forecast(
            probability=answer.probability,
            reasoning=answer.reasoning,
            extreme_justification=answer.extreme_justification,
            question=input.question,
            resolution_criteria=input.resolution_criteria,
            resolution_date=input.resolution_date,
            category=input.category,
            decompositions=decomposition.sub_questions,
        )
        violations = checks.run_forecast_checks(
            forecast, decomposition, outside, inside, sources_seen=deps.sources_seen
        )
        fixable = [
            v for v in checks.blocking(violations) if v.name in SYNTHESIS_FIXABLE
        ]
        if not fixable or attempts >= MAX_SYNTHESIS_ATTEMPTS:
            break

    anchor, _rule = checks.anchor_from(outside, decomposition)
    return SynthesisStepPayload(
        reflection=reflection,
        outside=outside,
        inside=inside,
        forecast=forecast,
        violations=violations,
        anchor=anchor if anchor is not None else outside.aggregate_base_rate,
        implied=checks.implied_probability(outside, inside, decomposition),
        derivation_slack=get_check_thresholds().derivation_slack,
        attempts=attempts,
    )


T = TypeVar("T")
U = TypeVar("U")


async def _gather_ok(
    items: list[T], fn: Callable[[T], Awaitable[U]]
) -> list[tuple[T, U]]:
    """Run `fn` over every item concurrently; keep the ones that did not raise."""
    results = await asyncio.gather(*(fn(i) for i in items), return_exceptions=True)
    return [(i, r) for i, r in zip(items, results) if not isinstance(r, BaseException)]


async def run_all(
    input: ForecastInput, *, emit=None, store=None
) -> tuple[Forecast, list[CheckViolation]]:
    """Every stage back-to-back with no gates. The CLI and eval entry point."""
    deps = ForecastDeps(emit=emit, store=store)

    decomposition = await run_decompose_stage(input, deps)
    researchable = [
        s for s in decomposition.sub_questions if s.knowability == "researchable"
    ]
    lens_sets = await _gather_ok(
        researchable, lambda s: run_lenses_stage(input, decomposition, s, deps)
    )
    cells = [(s, lens) for s, group in lens_sets for lens in group.lenses]
    measured = await _gather_ok(
        cells, lambda c: run_base_rate_step(input, c[0], c[1], deps)
    )
    base_rate_cells = [(s, p) for (s, _), p in measured]
    adjusted = await _gather_ok(
        base_rate_cells, lambda c: run_inside_step(input, c[0], c[1], deps)
    )
    inside_cells = [(s, p) for (s, _), p in adjusted]

    payload = await run_synthesis_stage(
        input, decomposition, base_rate_cells, inside_cells, deps
    )
    return payload.forecast, payload.violations
