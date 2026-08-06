"""The forecast graph — seven steps, three fan-outs, one retry cycle.

    decompose ─▶ choose lenses ─▶ research ─▶ adjust ─▶ reflect ─▶ synthesize ─▶ critique
                     ╱│╲            ╱│╲        ╱│╲                                  │
                 per sub-Q      per lens    per lens                    ▲           │
                     ╲│╱            ╲│╱        ╲│╱                      └───────────┘
                    join           join        join            (blocking violations, once)

Why the shape:

- **Principle 4 becomes structural.** "Outside view first" is a prompt instruction a model
  can ignore. As an edge it is impossible to violate — the adjust step reads the measured
  rate off state, so it cannot run before one exists.
- **Choosing populations is its own step**, and it runs with no rates in front of it. An
  agent that chose and measured in one pass could settle on whichever population gave the
  answer it already liked, and the output would look identical either way. Naming them
  blind is pre-registration.
- **The lens is the unit of parallelism.** Three lenses on five sub-questions is fifteen
  independent searches, not five.
- **The retry is a real cycle**, not an `if`. `critique` routes back through a decision.

Durability is DBOS's job. Every agent call goes through `durability.agent_step`, so a run
that dies resumes from the agent that died rather than the top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_graph.beta import GraphBuilder, StepContext
from pydantic_graph.beta.join import reduce_list_append
from pydantic_graph.beta.util import TypeExpression

from .. import checks, durability
from ..agents.decompose import run_decompose
from ..agents.inside_view import run_adjust_lens, whole_question_adjustments
from ..agents.lenses import run_choose_lenses
from ..agents.outside_view import (
    cell_deps,
    exhausted_notice,
    merge_base_rates,
    run_research_lens,
    whole_question_outside,
)
from ..agents.reflect import run_reflect
from ..agents.synthesize import run_synthesize
from ..deps import ForecastDeps
from ..models import (
    Adjustment,
    CheckViolation,
    Forecast,
    ForecastInput,
    InsideView,
    Lens,
    ResearchedLens,
    SourceRef,
    SubClaimAdjustments,
    SubClaimBaseRates,
    SubPrediction,
)
from .state import ForecastState

MAX_SYNTHESIS_ATTEMPTS = 2

STAGE_ORDER: tuple[str, ...] = (
    "decompose",
    "lenses",
    "outside",
    "inside",
    "reflect",
    "synth",
    "critique",
)
"""The stage keys the UI groups events under, in the order they run."""


@dataclass
class LensGroup:
    """One sub-question's chosen populations, on the way back from the first fan-out."""

    sub_claim: SubPrediction
    lenses: list[Lens] = field(default_factory=list)
    error: str = ""


@dataclass
class LensTask:
    """One (sub-question, population) pair, carried through both research fan-outs.

    `sources` travels with the task rather than being appended to the shared list as the
    cell goes: `observability` detects new sources by slicing the tail off
    `deps.sources_seen`, and concurrent cells writing to one list hand each other's
    sources to the wrong lens. The merge folds them in after the join, the only moment
    nothing else is writing.
    """

    sub_claim: SubPrediction
    lens: Lens
    researched: ResearchedLens | None = None
    already_controlled_for: str = ""
    adjustments: list[Adjustment] = field(default_factory=list)
    steel_man: str = ""
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

    Returns the researchable sub-questions, which is what the next row maps over. A
    `judgment` sub-question has, by its own label, no population to measure; it still
    gets a card and still contributes its own working estimate via `checks.chain_inputs`.
    """
    _stage(ctx.deps, "decompose")
    ctx.state.decomposition = await durability.agent_step(
        run_decompose, ctx.state.input, ctx.deps
    )
    _emit(ctx.deps, "decompose", ctx.state.decomposition.model_dump())
    _stage_end(ctx.deps, "decompose")

    _stage(ctx.deps, "lenses")
    return [
        s for s in ctx.state.decomposition.sub_claims if s.knowability == "researchable"
    ]


# ---------- choose lenses, one agent per sub-question ----------


@g.step
async def choose_lenses_cell(
    ctx: StepContext[ForecastState, ForecastDeps, SubPrediction],
) -> LensGroup:
    """Name populations for one sub-question. No tools, no rates."""
    sub_claim = ctx.inputs
    assert ctx.state.decomposition is not None
    try:
        result = await durability.agent_step(
            run_choose_lenses,
            ctx.state.input,
            ctx.state.decomposition,
            sub_claim,
            ctx.deps,
        )
        return LensGroup(sub_claim=sub_claim, lenses=list(result.lenses))
    except Exception as exc:
        return LensGroup(sub_claim=sub_claim, error=f"{type(exc).__name__}: {exc}")


@g.step
async def no_lens_cells(
    ctx: StepContext[ForecastState, ForecastDeps, list[SubPrediction]],
) -> list[LensGroup]:
    """The empty-row bypass.

    `.map()` over an empty list stalls the beta graph runner — it completes without
    producing a result — so a row with nothing to fan out over must not enter the fork.
    """
    return []


collect_lens_groups = g.join(reduce_list_append, initial_factory=list[LensGroup])


@g.step
async def merge_lenses(
    ctx: StepContext[ForecastState, ForecastDeps, list[LensGroup]],
) -> list[LensTask]:
    """Flatten to one task per (sub-question, population). The research maps over these."""
    tasks = [
        LensTask(sub_claim=group.sub_claim, lens=lens)
        for group in ctx.inputs
        for lens in group.lenses
    ]
    _emit(
        ctx.deps,
        "lenses",
        {
            "lenses": [
                {"sub_claim_id": t.sub_claim.id, **t.lens.model_dump()} for t in tasks
            ]
        },
    )
    _stage_end(ctx.deps, "lenses")

    _stage(ctx.deps, "outside")
    return tasks


# ---------- research, one agent per lens ----------


@g.step
async def research_lens_cell(
    ctx: StepContext[ForecastState, ForecastDeps, LensTask],
) -> LensTask:
    """Measure exactly one population. Searches; budget-limited."""
    task = ctx.inputs
    deps = cell_deps(
        ctx.deps, task.sub_claim.id or "", ctx.state.input.max_iterations
    )
    try:
        result: SubClaimBaseRates = await durability.agent_step(
            run_research_lens, ctx.state.input, task.sub_claim, task.lens, deps
        )
        # The weight and the population definition come from the *chosen* lens, never
        # from what came back — a research cell must not re-weight its own population
        # after seeing what it measured.
        researched = result.lens.model_copy(
            update={
                "name": task.lens.name,
                "population": task.lens.population,
                "why_it_fits": task.lens.why_it_fits,
                "weight": task.lens.weight,
                "weight_rationale": task.lens.weight_rationale,
            }
        )
        task.researched = researched
        task.already_controlled_for = result.disagreement
        task.sources = deps.sources_seen
        return task
    except Exception as exc:
        # A cell failing must not take its siblings down: the row degrades to whatever
        # the other populations found.
        if type(exc).__name__ == "UsageLimitExceeded":
            exhausted_notice(deps)
        task.sources = deps.sources_seen
        task.error = f"{type(exc).__name__}: {exc}"
        return task


@g.step
async def no_research_cells(
    ctx: StepContext[ForecastState, ForecastDeps, list[LensTask]],
) -> list[LensTask]:
    """The empty-row bypass. See `no_lens_cells`."""
    return []


collect_research = g.join(reduce_list_append, initial_factory=list[LensTask])


@g.step
async def merge_outside(
    ctx: StepContext[ForecastState, ForecastDeps, list[LensTask]],
) -> list[LensTask]:
    """The research barrier. Folds every measured population into one OutsideView.

    Returns the tasks whose lens actually landed — the ones the adjust row maps over. A
    population that was never measured has no rate to adjust *from*, which is principle
    5's whole premise.
    """
    tasks = list(ctx.inputs)
    for task in tasks:
        ctx.deps.sources_seen.extend(task.sources)

    found = [t for t in tasks if t.researched is not None]
    if not found:
        # Either nothing was researchable, or every lens failed. Both leave no outside
        # view to build.
        ctx.state.outside = await whole_question_outside(
            ctx.state.input,
            ctx.state.decomposition,
            ctx.deps,
            [t.error for t in tasks],
        )
    else:
        assert ctx.state.decomposition is not None
        ctx.state.outside = merge_base_rates(
            [t.sub_claim for t in found],
            [SubClaimBaseRates(lens=t.researched, disagreement=t.already_controlled_for) for t in found],
            ctx.state.decomposition,
        )

    ctx.state.sources_seen = list(ctx.deps.sources_seen)
    _emit(ctx.deps, "outside", ctx.state.outside.model_dump())
    _stage_end(ctx.deps, "outside")

    _stage(ctx.deps, "inside")
    # Re-read the lenses off state so the stamped `sub_claim_ids` travel with them.
    by_name = {l.name: l for l in ctx.state.outside.lenses}
    for t in found:
        t.researched = by_name.get(t.lens.name, t.researched)
    return found


# ---------- adjust, one agent per lens ----------


@g.step
async def adjust_lens_cell(
    ctx: StepContext[ForecastState, ForecastDeps, LensTask],
) -> LensTask:
    """Move exactly one population's rate by its own modifiers."""
    task = ctx.inputs
    assert task.researched is not None
    deps = cell_deps(
        ctx.deps, task.sub_claim.id or "", ctx.state.input.max_iterations
    )
    try:
        result: SubClaimAdjustments = await durability.agent_step(
            run_adjust_lens,
            ctx.state.input,
            task.sub_claim,
            task.researched,
            task.already_controlled_for,
            deps,
        )
        # Stamped by code for the same reason the lenses are: a cell moved exactly one
        # population, and a link it volunteered could point anywhere.
        task.adjustments = [
            a.model_copy(
                update={
                    "lens_name": task.lens.name,
                    "sub_claim_ids": [task.sub_claim.id],
                }
            )
            for a in result.adjustments
        ]
        task.steel_man = result.steel_man
        task.sources = deps.sources_seen
        return task
    except Exception as exc:
        if type(exc).__name__ == "UsageLimitExceeded":
            exhausted_notice(deps)
        task.sources = deps.sources_seen
        task.error = f"{type(exc).__name__}: {exc}"
        return task


@g.step
async def no_adjust_cells(
    ctx: StepContext[ForecastState, ForecastDeps, list[LensTask]],
) -> list[LensTask]:
    """The empty-row bypass. See `no_lens_cells`."""
    return []


collect_adjustments = g.join(reduce_list_append, initial_factory=list[LensTask])


@g.step
async def merge_inside(
    ctx: StepContext[ForecastState, ForecastDeps, list[LensTask]],
) -> None:
    """The adjust barrier. Collects every population's moves."""
    tasks = list(ctx.inputs)
    for task in tasks:
        ctx.deps.sources_seen.extend(task.sources)

    adjustments: list[Adjustment] = []
    steel_mans: dict[str, str] = {}
    for task in tasks:
        adjustments.extend(task.adjustments)
        if task.steel_man:
            steel_mans[task.lens.name] = task.steel_man

    if not adjustments:
        assert ctx.state.outside is not None
        adjustments, steel_mans = await whole_question_adjustments(
            ctx.state.input, ctx.state.outside, ctx.deps, [t.error for t in tasks]
        )

    ctx.state.adjustments = adjustments
    ctx.state.steel_mans = steel_mans
    ctx.state.sources_seen = list(ctx.deps.sources_seen)
    _stage_end(ctx.deps, "inside")


# ---------- reflect ----------


@g.step
async def reflect(ctx: StepContext[ForecastState, ForecastDeps, None]) -> None:
    """Principles 14 and 15 — the case against, and the bias sweep.

    Its own step rather than a tail call, because it is an agent run and every agent run
    should be a node: it gets its own stage in the UI, its own span in the trace, and its
    own durable checkpoint. It reads every population's counter-argument together, which
    is exactly why it cannot be asked of one lens.
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
    # Each fan-out routes through a decision so an empty row bypasses the fork rather
    # than stalling in it — mapping over an empty list hangs the beta runner.
    g.edge_from(decompose).to(
        g.decision()
        .branch(g.match(list, matches=bool).map().to(choose_lenses_cell))
        .branch(g.match(list).to(no_lens_cells))
    ),
    g.edge_from(choose_lenses_cell).to(collect_lens_groups),
    g.edge_from(collect_lens_groups, no_lens_cells).to(merge_lenses),
    g.edge_from(merge_lenses).to(
        g.decision()
        .branch(g.match(list, matches=bool).map().to(research_lens_cell))
        .branch(g.match(list).to(no_research_cells))
    ),
    g.edge_from(research_lens_cell).to(collect_research),
    g.edge_from(collect_research, no_research_cells).to(merge_outside),
    g.edge_from(merge_outside).to(
        g.decision()
        .branch(g.match(list, matches=bool).map().to(adjust_lens_cell))
        .branch(g.match(list).to(no_adjust_cells))
    ),
    g.edge_from(adjust_lens_cell).to(collect_adjustments),
    g.edge_from(collect_adjustments, no_adjust_cells).to(merge_inside),
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
    caller keep the reference the graph mutates.
    """
    deps = ForecastDeps(as_of=as_of, model=model, verbose=verbose, emit=emit)
    state = state if state is not None else ForecastState(input=input)

    forecast = await forecast_graph.run(state=state, deps=deps)
    return forecast, state.violations


def forecast_mermaid() -> str:
    """The real graph as mermaid. Backs `superforecaster diagram`."""
    return forecast_graph.render()
