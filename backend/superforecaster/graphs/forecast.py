"""The forecast graph — five nodes, one retry loop.

    Decompose -> FindBaseRates -> AdjustInsideView -> Synthesize -> Critique
                                                          ^            |
                                                          +------------+
                                                        (blocking violations, once)

Why a graph rather than four function calls in a row:

- **Principle 4 becomes structural.** "Outside view first" is a prompt instruction a
  model can ignore. As the edge `FindBaseRates -> AdjustInsideView` it is impossible
  to violate — the inside-view agent takes the base rate as an argument, so it cannot
  run before one exists.
- **The retry is a real cycle**, not an `if`. `Critique` routes back to `Synthesize`
  with the specific failed check attached.
- **The wiring is inspectable.** `forecast_mermaid()` renders the actual graph, so the
  diagram in the spec cannot drift from the code.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from .. import checks
from ..agents.decompose import run_decompose
from ..agents.inside_view import run_inside_view
from ..agents.outside_view import run_outside_view
from ..agents.synthesize import run_synthesize
from ..models import CheckViolation, Forecast, ForecastInput
from .state import ForecastDeps, ForecastState

MAX_SYNTHESIS_ATTEMPTS = 2

STAGE_KEYS: dict[str, str] = {
    "Decompose": "decompose",
    "FindBaseRates": "outside",
    "AdjustInsideView": "inside",
    "Synthesize": "synth",
    "Critique": "critique",
}
"""Node class name -> the short stage key the UI groups events under."""


class GraphHooks(Protocol):
    """Observation points on a graph run.

    Implemented by `runs.Run`, and None for the CLI, cron, and evals — which is what
    keeps the streaming machinery from being something every caller has to know about.
    """

    def stage_started(self, stage: str, attempt: int) -> None:
        """Called before a node runs."""

    def stage_finished(self, stage: str, state: ForecastState) -> None:
        """Called after a node runs, with the field it just wrote already on `state`."""


@dataclass
class Decompose(BaseNode[ForecastState, ForecastDeps, Forecast]):
    """Principles 1 and 2 — Fermi-ize, and label what is researchable."""

    async def run(
        self, ctx: GraphRunContext[ForecastState, ForecastDeps]
    ) -> FindBaseRates:
        ctx.state.decomposition = await run_decompose(ctx.state.input, ctx.deps)
        return FindBaseRates()


@dataclass
class FindBaseRates(BaseNode[ForecastState, ForecastDeps, Forecast]):
    """Principles 4 and 7 — reference classes and their base rates.

    This node running before AdjustInsideView is the whole of principle 4.
    """

    async def run(
        self, ctx: GraphRunContext[ForecastState, ForecastDeps]
    ) -> AdjustInsideView:
        assert ctx.state.decomposition is not None
        ctx.state.outside = await run_outside_view(
            ctx.state.input, ctx.state.decomposition, ctx.deps
        )
        # Snapshot after every node that searches, not only in `Critique`: the outside
        # stage projects its base rates immediately, and it resolves each cited URL
        # against what was actually retrieved to say which search found it.
        ctx.state.sources_seen = list(ctx.deps.sources_seen)
        return AdjustInsideView()


@dataclass
class AdjustInsideView(BaseNode[ForecastState, ForecastDeps, Forecast]):
    """Principles 5, 9, 14, 15 — signed adjustments away from the base rate."""

    async def run(
        self, ctx: GraphRunContext[ForecastState, ForecastDeps]
    ) -> Synthesize:
        assert ctx.state.outside is not None
        assert ctx.state.decomposition is not None
        ctx.state.inside = await run_inside_view(
            ctx.state.input, ctx.state.decomposition, ctx.state.outside, ctx.deps
        )
        ctx.state.sources_seen = list(ctx.deps.sources_seen)
        return Synthesize()


@dataclass
class Synthesize(BaseNode[ForecastState, ForecastDeps, Forecast]):
    """Principles 6, 8, 16 — commit to a number.

    Receives `state.violations` so a second attempt is a correction, not a re-roll.
    """

    async def run(self, ctx: GraphRunContext[ForecastState, ForecastDeps]) -> Critique:
        assert ctx.state.decomposition is not None
        assert ctx.state.outside is not None
        assert ctx.state.inside is not None
        ctx.state.synthesis_attempts += 1
        ctx.state.forecast = await run_synthesize(
            ctx.state.input,
            ctx.state.decomposition,
            ctx.state.outside,
            ctx.state.inside,
            ctx.state.violations,
            ctx.deps,
        )
        return Critique()


@dataclass
class Critique(BaseNode[ForecastState, ForecastDeps, Forecast]):
    """Pure — no LLM. Runs the methodology checks and decides whether to retry.

    Surviving violations travel out with the result rather than being swallowed, so a
    forecast that failed a check twice is still returned but is visibly flawed.
    """

    async def run(
        self, ctx: GraphRunContext[ForecastState, ForecastDeps]
    ) -> Synthesize | End[Forecast]:
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

        blocking = checks.blocking(ctx.state.violations)
        if blocking and ctx.state.synthesis_attempts < MAX_SYNTHESIS_ATTEMPTS:
            return Synthesize()
        return End(ctx.state.forecast)


forecast_graph = Graph(
    nodes=[Decompose, FindBaseRates, AdjustInsideView, Synthesize, Critique],
    state_type=ForecastState,
    run_end_type=Forecast,
)


async def run_forecast_graph(
    input: ForecastInput,
    *,
    as_of=None,
    model: str | None = None,
    verbose: bool = False,
    hooks: GraphHooks | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    persistence: Any = None,
    resume: bool = False,
) -> tuple[Forecast, list[CheckViolation]]:
    """Run the forecast pipeline. The single entry point for API, CLI, and evals.

    Returns the forecast plus any violations that survived the retry, so a caller can
    tell a clean forecast from one that never satisfied its own methodology.

    Question metadata is re-stamped from the input afterwards — the model has no
    business restating it, and letting it try invites drift.

    `hooks` and `emit` are the streaming seam and both default to off, in which case
    this drives exactly the nodes `forecast_graph.run()` did. `hooks` fires per node;
    `emit` rides on `ForecastDeps` down to the agents' own event stream handler, so
    tool calls and token deltas surface without any agent knowing about it.

    `persistence` snapshots state around every node, so a run that dies part-way can be
    picked up from its last completed node rather than paying for the whole graph
    again. `resume=True` restores from those snapshots instead of starting at
    `Decompose` — the caller is responsible for having rewound a failed snapshot first
    (see `checkpoints.rewind_for_resume`).
    """
    deps = ForecastDeps(as_of=as_of, model=model, verbose=verbose, emit=emit)
    state = ForecastState(input=input)

    if hooks is None and persistence is None:
        result = await forecast_graph.run(Decompose(), state=state, deps=deps)
        output = result.output
    else:
        output = await _run_with_hooks(state, deps, hooks, persistence, resume)

    forecast = output.model_copy(
        update={
            "question": input.question,
            "resolution_criteria": input.resolution_criteria,
            "resolution_date": input.resolution_date,
            "category": input.category,
        }
    )
    return forecast, state.violations


@asynccontextmanager
async def _graph_run(
    state: ForecastState, deps: ForecastDeps, persistence: Any, resume: bool
):
    """Either a fresh walk from `Decompose`, or one restored from snapshots.

    On resume the state comes out of persistence, not the `state` argument — that is
    the point, and it is why the caller does not need to rebuild what already ran.
    """
    if resume:
        if persistence is None:
            raise ValueError("resume requires a persistence backend")
        async with forecast_graph.iter_from_persistence(
            persistence, deps=deps
        ) as graph_run:
            yield graph_run
    else:
        async with forecast_graph.iter(
            Decompose(), state=state, deps=deps, persistence=persistence
        ) as graph_run:
            yield graph_run


async def _run_with_hooks(
    state: ForecastState,
    deps: ForecastDeps,
    hooks: GraphHooks | None,
    persistence: Any = None,
    resume: bool = False,
) -> Forecast:
    """Drive the graph one node at a time so each transition can be observed.

    `stage_started` fires before the node's agent is called and `stage_finished` after,
    which is the only ordering that lets a UI show a stage as busy while it works.

    The retry edge needs no special handling: `Synthesize` is simply yielded twice, and
    `synthesis_attempts` — which the node itself increments — tells the two apart.
    """
    async with _graph_run(state, deps, persistence, resume) as graph_run:
        live_state = graph_run.state
        if resume:
            # The restored snapshot carries the OLD budget, and the research agents read
            # it off graph state (`ctx.state.input`) rather than the caller's. Without
            # this, resuming with a higher search depth re-runs into the same
            # UsageLimitExceeded — while the UI reports the depth the caller asked for.
            live_state.input = live_state.input.model_copy(
                update={"max_iterations": state.input.max_iterations}
            )
        node = graph_run.next_node
        while not isinstance(node, End):
            stage = STAGE_KEYS[type(node).__name__]
            if hooks:
                hooks.stage_started(stage, _attempt_for(stage, live_state))
            node = await graph_run.next(node)
            if hooks:
                hooks.stage_finished(stage, live_state)
        result = graph_run.result

    assert result is not None
    # A resumed run's state lives in the GraphRun, not the caller's `state` — copy the
    # violations back so `run_forecast_graph` reports them either way.
    state.violations = list(live_state.violations)
    return result.output


def _attempt_for(stage: str, state: ForecastState) -> int:
    """Which attempt a stage is about to make.

    Only Synthesize and the Critique that judges it can repeat. `synthesis_attempts` is
    incremented by the Synthesize node, so before it runs the attempt is one higher and
    after it runs — which is when Critique starts — it is already correct.
    """
    if stage == "synth":
        return state.synthesis_attempts + 1
    if stage == "critique":
        return max(1, state.synthesis_attempts)
    return 1


def forecast_mermaid() -> str:
    """The real graph as mermaid. Backs `superforecaster diagram`."""
    return forecast_graph.mermaid_code(start_node=Decompose)
