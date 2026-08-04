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

from dataclasses import dataclass

from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from .. import checks
from ..agents.decompose import run_decompose
from ..agents.inside_view import run_inside_view
from ..agents.outside_view import run_outside_view
from ..agents.synthesize import run_synthesize
from ..models import CheckViolation, Forecast, ForecastInput
from .state import ForecastDeps, ForecastState

MAX_SYNTHESIS_ATTEMPTS = 2


@dataclass
class Decompose(BaseNode[ForecastState, ForecastDeps, Forecast]):
    """Principles 1 and 2 — Fermi-ize, and label what is researchable."""

    async def run(self, ctx: GraphRunContext[ForecastState, ForecastDeps]) -> FindBaseRates:
        ctx.state.decomposition = await run_decompose(ctx.state.input, ctx.deps)
        return FindBaseRates()


@dataclass
class FindBaseRates(BaseNode[ForecastState, ForecastDeps, Forecast]):
    """Principles 4 and 7 — reference classes and their base rates.

    This node running before AdjustInsideView is the whole of principle 4.
    """

    async def run(self, ctx: GraphRunContext[ForecastState, ForecastDeps]) -> AdjustInsideView:
        assert ctx.state.decomposition is not None
        ctx.state.outside = await run_outside_view(
            ctx.state.input, ctx.state.decomposition, ctx.deps
        )
        return AdjustInsideView()


@dataclass
class AdjustInsideView(BaseNode[ForecastState, ForecastDeps, Forecast]):
    """Principles 5, 9, 14, 15 — signed adjustments away from the base rate."""

    async def run(self, ctx: GraphRunContext[ForecastState, ForecastDeps]) -> Synthesize:
        assert ctx.state.outside is not None
        ctx.state.inside = await run_inside_view(
            ctx.state.input, ctx.state.outside, ctx.deps
        )
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

        ctx.state.violations = checks.run_forecast_checks(
            ctx.state.forecast,
            ctx.state.decomposition,
            ctx.state.outside,
            ctx.state.inside,
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
) -> tuple[Forecast, list[CheckViolation]]:
    """Run the forecast pipeline. The single entry point for API, CLI, and evals.

    Returns the forecast plus any violations that survived the retry, so a caller can
    tell a clean forecast from one that never satisfied its own methodology.

    Question metadata is re-stamped from the input afterwards — the model has no
    business restating it, and letting it try invites drift.
    """
    deps = ForecastDeps(as_of=as_of, model=model, verbose=verbose)
    state = ForecastState(input=input)

    result = await forecast_graph.run(Decompose(), state=state, deps=deps)
    forecast = result.output.model_copy(
        update={
            "question": input.question,
            "resolution_criteria": input.resolution_criteria,
            "resolution_date": input.resolution_date,
            "category": input.category,
        }
    )
    return forecast, state.violations


def forecast_mermaid() -> str:
    """The real graph as mermaid. Backs `superforecaster diagram`."""
    return forecast_graph.mermaid_code(start_node=Decompose)
