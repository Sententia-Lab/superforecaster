"""The forecast graph — six steps, two fan-outs, one retry cycle.

    decompose ──▶ base rates ──▶ inside view ──▶ reflect ──▶ synthesize ──▶ critique
                     ╱│╲            ╱│╲                          ▲             │
                 map over        map over                        └─────────────┘
                sub-claims      sub-claims                    (blocking violations, once)
                     ╲│╱            ╲│╲
                    join           join

Why a graph rather than six function calls in a row:

- **Principle 4 becomes structural.** "Outside view first" is a prompt instruction a
  model can ignore. As an edge it is impossible to violate — the inside-view step reads
  the base rate off state, so it cannot run before one exists.
- **The retry is a real cycle**, not an `if`. `critique` routes back to `synthesize` with
  the specific failed check attached.
- **The fan-out is declared, not hand-rolled.** Each research row is one `.map()` edge
  and one join, so "one agent per sub-question, then a barrier" is something you read off
  the graph rather than reconstruct from `asyncio.gather` and a `zip`.

Durability is DBOS's job, not this module's. Every agent call goes through
`durability.agent_step`, which makes it a checkpointed step when the process is
checkpointing, so a run that dies resumes from the agent that died rather than the top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_graph.beta import GraphBuilder, StepContext
from pydantic_graph.beta.join import reduce_list_append
from pydantic_graph.beta.util import TypeExpression

from .. import checks
from ..agents.decompose import run_decompose
from ..agents.inside_view import run_inside_view_cell, whole_question_adjustments
from ..agents.outside_view import (
    cell_deps,
    exhausted_notice,
    merge_base_rates,
    run_base_rate_cell,
    whole_question_outside,
)
from ..agents.reflect import run_reflect
from ..agents.synthesize import run_synthesize
from .. import durability
from ..deps import ForecastDeps
from ..models import (
    Adjustment,
    CheckViolation,
    Forecast,
    ForecastInput,
    InsideView,
    SourceRef,
    SubClaimAdjustments,
    SubClaimBaseRates,
    SubPrediction,
)
from .state import ForecastState

MAX_SYNTHESIS_ATTEMPTS = 2

STAGE_ORDER: tuple[str, ...] = (
    "decompose",
    "outside",
    "inside",
    "reflect",
    "synth",
    "critique",
)
"""The stage keys the UI groups events under, in the order they run."""


@dataclass
class Cell:
    """One column's work, carried back to the barrier.

    `sources` travels with the result rather than being appended to the shared list as
    the cell goes: `observability` detects new sources by slicing the tail off
    `deps.sources_seen`, and concurrent cells writing to one list hand each other's
    sources to the wrong column. The merge step folds them in after the join, which is
    the only moment nothing else is writing.
    """

    sub_claim: SubPrediction
    result: Any | None = None
    sources: list[SourceRef] = field(default_factory=list)
    error: str = ""


def _emit(
    deps: ForecastDeps, type: str, payload: dict[str, Any], sub_claim: str | None = None
) -> None:
    if deps.emit is not None:
        deps.emit(type, payload, sub_claim)


def _stage(deps: ForecastDeps, stage: str, attempt: int = 1) -> None:
    """Open a stage. The UI groups every following event under it until the next one."""
    _emit(deps, "stage", {"stage": stage, "attempt": attempt})


def _stage_end(deps: ForecastDeps, stage: str) -> None:
    _emit(deps, "stage_end", {"stage": stage})


g = GraphBuilder(
    state_type=ForecastState,
    deps_type=ForecastDeps,
    output_type=Forecast,
)


# ---------- decompose ----------


@g.step
async def decompose(
    ctx: StepContext[ForecastState, ForecastDeps, None],
) -> list[SubPrediction]:
    """Principles 1 and 2 — Fermi-ize, and label what is researchable.

    Returns the researchable columns, which is what the next row maps over. A `judgment`
    column has, by its own label, no base rate to look up; it still gets a card and still
    contributes its own working estimate via `checks.chain_inputs`.
    """
    _stage(ctx.deps, "decompose")
    ctx.state.decomposition = await durability.agent_step(
        run_decompose, ctx.state.input, ctx.deps
    )
    _emit(ctx.deps, "decompose", ctx.state.decomposition.model_dump())
    _stage_end(ctx.deps, "decompose")

    _stage(ctx.deps, "outside")
    return [s for s in ctx.state.decomposition.sub_claims if s.knowability == "researchable"]


# ---------- base rates, one agent per column ----------


@g.step
async def base_rate_cell(
    ctx: StepContext[ForecastState, ForecastDeps, SubPrediction],
) -> Cell:
    """Research base rates for exactly one column. Searches; budget-limited."""
    sub_claim = ctx.inputs
    assert ctx.state.decomposition is not None
    deps = cell_deps(ctx.deps, sub_claim.id or "", ctx.state.input.max_iterations)
    try:
        result = await durability.agent_step(
            run_base_rate_cell, ctx.state.input, ctx.state.decomposition, sub_claim, deps
        )
        return Cell(sub_claim=sub_claim, result=result, sources=deps.sources_seen)
    except Exception as exc:
        # A cell failing must not take its siblings down: the row degrades to whatever
        # the other columns found, and this one falls back to its own working estimate.
        if type(exc).__name__ == "UsageLimitExceeded":
            exhausted_notice(deps)
        return Cell(
            sub_claim=sub_claim,
            sources=deps.sources_seen,
            error=f"{type(exc).__name__}: {exc}",
        )


@g.step
async def no_base_rate_cells(
    ctx: StepContext[ForecastState, ForecastDeps, list[SubPrediction]],
) -> list[Cell]:
    """The empty-row bypass.

    `.map()` over an empty list stalls the beta graph runner — it completes without
    producing a result — so a row with no columns to research must not enter the fork at
    all. Routing to an empty barrier instead keeps `merge_outside` the single place that
    decides what to do about it.
    """
    return []


collect_base_rates = g.join(reduce_list_append, initial_factory=list[Cell])


@g.step
async def merge_outside(
    ctx: StepContext[ForecastState, ForecastDeps, list[Cell]],
) -> list[SubPrediction]:
    """The base-rate barrier. Folds every column's classes into one OutsideView.

    Returns the columns that ended up with at least one reference class — the ones the
    inside-view row maps over. A column with nothing researched has no base rate to
    adjust *from*, which is principle 5's whole premise.
    """
    cells = list(ctx.inputs)
    for cell in cells:
        ctx.deps.sources_seen.extend(cell.sources)

    found = [c for c in cells if isinstance(c.result, SubClaimBaseRates)]
    if not found:
        # Either nothing was labelled researchable, or every column failed. Both leave
        # no outside view to build, and `OutsideView` requires two classes.
        ctx.state.outside = await whole_question_outside(
            ctx.state.input, ctx.state.decomposition, ctx.deps, [c.error for c in cells]
        )
    else:
        assert ctx.state.decomposition is not None
        ctx.state.outside = merge_base_rates(
            [c.sub_claim for c in found],
            [c.result for c in found],
            ctx.state.decomposition,
        )

    ctx.state.sources_seen = list(ctx.deps.sources_seen)
    _emit(ctx.deps, "outside", ctx.state.outside.model_dump())
    _stage_end(ctx.deps, "outside")

    _stage(ctx.deps, "inside")
    assert ctx.state.decomposition is not None
    return [
        s
        for s in ctx.state.decomposition.sub_claims
        if s.id and checks.classes_for(s.id, ctx.state.outside)
    ]


# ---------- inside view, one agent per column ----------


@g.step
async def inside_cell(
    ctx: StepContext[ForecastState, ForecastDeps, SubPrediction],
) -> Cell:
    """Adjust from ONE column's base rate. Searches; budget-limited."""
    sub_claim = ctx.inputs
    assert ctx.state.outside is not None
    deps = cell_deps(ctx.deps, sub_claim.id or "", ctx.state.input.max_iterations)
    try:
        result = await durability.agent_step(
            run_inside_view_cell, ctx.state.input, sub_claim, ctx.state.outside, deps
        )
        return Cell(sub_claim=sub_claim, result=result, sources=deps.sources_seen)
    except Exception as exc:
        if type(exc).__name__ == "UsageLimitExceeded":
            exhausted_notice(deps)
        return Cell(
            sub_claim=sub_claim,
            sources=deps.sources_seen,
            error=f"{type(exc).__name__}: {exc}",
        )


@g.step
async def no_inside_cells(
    ctx: StepContext[ForecastState, ForecastDeps, list[SubPrediction]],
) -> list[Cell]:
    """The same bypass for the inside-view row. See `no_base_rate_cells`."""
    return []


collect_adjustments = g.join(reduce_list_append, initial_factory=list[Cell])


@g.step
async def merge_inside(
    ctx: StepContext[ForecastState, ForecastDeps, list[Cell]],
) -> None:
    """The inside-view barrier. Stamps every adjustment with the column that made it.

    Stamped by code for the same reason the reference classes are: a cell worked on
    exactly one column, and a link it volunteered could point anywhere.
    """
    cells = list(ctx.inputs)
    for cell in cells:
        ctx.deps.sources_seen.extend(cell.sources)

    adjustments: list[Adjustment] = []
    steel_mans: dict[str, str] = {}
    for cell in cells:
        if not isinstance(cell.result, SubClaimAdjustments):
            continue
        adjustments.extend(
            a.model_copy(update={"sub_claim_ids": [cell.sub_claim.id]})
            for a in cell.result.adjustments
        )
        steel_mans[cell.sub_claim.id or ""] = cell.result.steel_man

    if not adjustments:
        # No column carried a reference class, or every one of them failed. Adjust from
        # the whole-question anchor rather than crashing.
        assert ctx.state.outside is not None
        adjustments, steel_mans = await whole_question_adjustments(
            ctx.state.input, ctx.state.outside, ctx.deps, [c.error for c in cells]
        )

    ctx.state.adjustments = adjustments
    ctx.state.steel_mans = steel_mans
    ctx.state.sources_seen = list(ctx.deps.sources_seen)
    _stage_end(ctx.deps, "inside")


# ---------- reflect ----------


@g.step
async def reflect(ctx: StepContext[ForecastState, ForecastDeps, None]) -> None:
    """Principles 14 and 15 — the case against, and the bias sweep.

    Its own step rather than a tail call inside the inside-view row, because it is an
    agent run and every agent run should be a node: it gets its own stage in the UI, its
    own span in the trace, and its own DBOS checkpoint. It reads every column's
    counter-argument together, which is exactly why it cannot be asked of one column.
    """
    _stage(ctx.deps, "reflect")
    assert ctx.state.decomposition is not None
    assert ctx.state.outside is not None

    reflection = await durability.agent_step(
        run_reflect,
        ctx.state.input,
        ctx.state.decomposition,
        ctx.state.outside,
        ctx.state.adjustments,
        ctx.state.steel_mans,
        ctx.deps,
    )
    ctx.state.inside = InsideView(
        adjustments=ctx.state.adjustments,
        steel_man=reflection.steel_man,
        what_would_change_my_mind=reflection.what_would_change_my_mind,
        bias_checks=reflection.bias_checks,
    )
    _emit(ctx.deps, "inside", ctx.state.inside.model_dump())
    _stage_end(ctx.deps, "reflect")


# ---------- synthesize, critique, and the retry cycle ----------


@g.step
async def synthesize(ctx: StepContext[ForecastState, ForecastDeps, None]) -> None:
    """Principles 6, 8, 16 — commit to a number.

    Reads `state.violations` so a second attempt is a correction, not a re-roll.
    """
    ctx.state.synthesis_attempts += 1
    _stage(ctx.deps, "synth", ctx.state.synthesis_attempts)

    assert ctx.state.decomposition is not None
    assert ctx.state.outside is not None
    assert ctx.state.inside is not None
    ctx.state.forecast = await durability.agent_step(
        run_synthesize,
        ctx.state.input,
        ctx.state.decomposition,
        ctx.state.outside,
        ctx.state.inside,
        ctx.state.violations,
        ctx.deps,
    )
    _emit(ctx.deps, "synth", ctx.state.forecast.model_dump())
    _stage_end(ctx.deps, "synth")


@g.step
async def critique(
    ctx: StepContext[ForecastState, ForecastDeps, None],
) -> Literal["retry", "accept"]:
    """Pure — no LLM. Runs the methodology checks and decides whether to retry.

    Surviving violations travel out with the result rather than being swallowed, so a
    forecast that failed a check twice is still returned but is visibly flawed.
    """
    _stage(ctx.deps, "critique", max(1, ctx.state.synthesis_attempts))
    assert ctx.state.forecast is not None
    assert ctx.state.decomposition is not None
    assert ctx.state.outside is not None
    assert ctx.state.inside is not None

    ctx.state.sources_seen = list(ctx.deps.sources_seen)
    ctx.state.violations = checks.run_forecast_checks(
        ctx.state.forecast,
        ctx.state.decomposition,
        ctx.state.outside,
        ctx.state.inside,
        sources_seen=ctx.state.sources_seen,
    )
    _emit(
        ctx.deps,
        "critique",
        {"violations": [v.model_dump() for v in ctx.state.violations]},
    )
    _stage_end(ctx.deps, "critique")

    blocking = checks.blocking(ctx.state.violations)
    if blocking and ctx.state.synthesis_attempts < MAX_SYNTHESIS_ATTEMPTS:
        return "retry"
    return "accept"


@g.step
async def finish(ctx: StepContext[ForecastState, ForecastDeps, None]) -> Forecast:
    """Re-stamp the question metadata the caller supplied.

    The model has no business restating it, and letting it try invites drift.
    """
    assert ctx.state.forecast is not None
    return ctx.state.forecast.model_copy(
        update={
            "question": ctx.state.input.question,
            "resolution_criteria": ctx.state.input.resolution_criteria,
            "resolution_date": ctx.state.input.resolution_date,
            "category": ctx.state.input.category,
        }
    )


g.add(
    g.edge_from(g.start_node).to(decompose),
    # `.map()` is the fan-out: one cell task per column. The empty branch exists because
    # mapping over an empty list stalls the runner — see `no_base_rate_cells`.
    g.edge_from(decompose).to(
        g.decision()
        .branch(g.match(list, matches=bool).map().to(base_rate_cell))
        .branch(g.match(list).to(no_base_rate_cells))
    ),
    g.edge_from(base_rate_cell).to(collect_base_rates),
    g.edge_from(collect_base_rates, no_base_rate_cells).to(merge_outside),
    g.edge_from(merge_outside).to(
        g.decision()
        .branch(g.match(list, matches=bool).map().to(inside_cell))
        .branch(g.match(list).to(no_inside_cells))
    ),
    g.edge_from(inside_cell).to(collect_adjustments),
    g.edge_from(collect_adjustments, no_inside_cells).to(merge_inside),
    g.edge_from(merge_inside).to(reflect),
    g.edge_from(reflect).to(synthesize),
    g.edge_from(synthesize).to(critique),
    g.edge_from(critique).to(
        g.decision()
        .branch(g.match(TypeExpression[Literal["retry"]]).to(synthesize))
        .branch(g.match(TypeExpression[Literal["accept"]]).to(finish))
    ),
    g.edge_from(finish).to(g.end_node),
)

forecast_graph = g.build()


async def run_forecast_graph(
    input: ForecastInput,
    *,
    as_of=None,
    model: str | None = None,
    verbose: bool = False,
    emit=None,
    state: ForecastState | None = None,
) -> tuple[Forecast, list[CheckViolation]]:
    """Run the forecast pipeline. The single entry point for API, CLI, and evals.

    Returns the forecast plus any violations that survived the retry, so a caller can
    tell a clean forecast from one that never satisfied its own methodology.

    `emit` rides on `ForecastDeps` down to the agents' own event stream handler, so tool
    calls and token deltas surface without any agent knowing about it. `state` lets a
    caller keep the reference the graph mutates — a streamed run needs it to build the
    final result frame.
    """
    deps = ForecastDeps(as_of=as_of, model=model, verbose=verbose, emit=emit)
    state = state if state is not None else ForecastState(input=input)

    forecast = await forecast_graph.run(state=state, deps=deps)
    return forecast, state.violations


def forecast_mermaid() -> str:
    """The real graph as mermaid. Backs `superforecaster diagram`."""
    return forecast_graph.render()
