"""The daily update graph — four nodes, one verification loop.

    CheckResolved --resolved--> End(flagged)
         |
      not resolved
         v
    ApplyBayes -> GuardUpdate -> End
                      ^   |
                      |   v (large move, unverified)
                   VerifyLargeMove

Two things this expresses that the previous two-`for`-loop version could not:

- **Resolution blocks the update** as a graph edge, not as a `flagged_ids` set passed
  between loops. A forecast that has already resolved cannot have its probability
  moved, because the node that would move it is unreachable.
- **A big jump routes through verification** rather than being capped. Decisive events
  are real; the response to one is to corroborate it, not to forbid the move.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import get_check_thresholds
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

from .. import checks, db
from ..agents.resolution import run_resolution_check
from ..agents.update import run_update
from ..models import UpdateOutcome
from .state import ForecastDeps, UpdateState

MAX_VERIFY_ATTEMPTS = 1


@dataclass
class CheckResolved(BaseNode[UpdateState, ForecastDeps, UpdateOutcome]):
    """Has this already resolved? If so, nothing else may touch it."""

    async def run(
        self, ctx: GraphRunContext[UpdateState, ForecastDeps]
    ) -> ApplyBayes | End[UpdateOutcome]:
        result = await run_resolution_check(ctx.state.record, ctx.deps)
        ctx.state.resolution = result
        db.mark_refreshed(ctx.state.record.id, flagged=result.appears_resolved)

        if result.appears_resolved:
            return End(
                UpdateOutcome(
                    flagged_resolved=True,
                    updated=False,
                    reason=(
                        "flagged for resolution review; probability update skipped — "
                        f"{result.reasoning}"
                    ),
                )
            )
        return ApplyBayes()


@dataclass
class ApplyBayes(BaseNode[UpdateState, ForecastDeps, UpdateOutcome]):
    """Principles 10 and 11 — should the probability move, and which way?"""

    async def run(self, ctx: GraphRunContext[UpdateState, ForecastDeps]) -> GuardUpdate:
        ctx.state.decision = await run_update(ctx.state.record, ctx.deps)
        return GuardUpdate()


@dataclass
class VerifyLargeMove(BaseNode[UpdateState, ForecastDeps, UpdateOutcome]):
    """Principle 12 — corroborate a big jump before writing it.

    Not a cap. FTX filing for bankruptcy is a legitimate 0.20 -> 0.99 move; the point
    is to check the claim holds up, not to average it back toward the prior.
    """

    async def run(self, ctx: GraphRunContext[UpdateState, ForecastDeps]) -> GuardUpdate:
        assert ctx.state.decision is not None
        prior = ctx.state.decision.prior
        posterior = ctx.state.decision.posterior

        ctx.state.verify_attempts += 1
        revised = await run_update(
            ctx.state.record, ctx.deps, verify=(prior, posterior)
        )
        ctx.state.decision = revised.model_copy(update={"verified_large_move": True})
        return GuardUpdate()


@dataclass
class GuardUpdate(BaseNode[UpdateState, ForecastDeps, UpdateOutcome]):
    """Pure — no LLM. Routes large moves to verification, then gates the write."""

    async def run(
        self, ctx: GraphRunContext[UpdateState, ForecastDeps]
    ) -> VerifyLargeMove | End[UpdateOutcome]:
        assert ctx.state.decision is not None
        decision = ctx.state.decision
        thresholds = get_check_thresholds()

        if (
            checks.is_large_move(decision, thresholds)
            and ctx.state.verify_attempts < MAX_VERIFY_ATTEMPTS
        ):
            return VerifyLargeMove()

        ctx.state.violations = checks.run_update_checks(decision, thresholds)

        delta = abs(decision.posterior - decision.prior)
        if delta < thresholds.min_probability_delta:
            return End(
                UpdateOutcome(
                    updated=False,
                    violations=ctx.state.violations,
                    reason=(
                        f"change of {delta:.3f} is below the "
                        f"{thresholds.min_probability_delta:.3f} threshold — treated as noise"
                    ),
                )
            )

        if checks.blocking(ctx.state.violations):
            names = ", ".join(v.name for v in ctx.state.violations)
            return End(
                UpdateOutcome(
                    updated=False,
                    new_probability=decision.posterior,
                    violations=ctx.state.violations,
                    reason=f"update is internally inconsistent ({names}); not written",
                )
            )

        db.add_forecast_update(
            forecast_id=ctx.state.record.id,
            probability=decision.posterior,
            confidence="medium",
            reasoning=decision.reasoning,
        )
        return End(
            UpdateOutcome(
                updated=True,
                new_probability=decision.posterior,
                violations=ctx.state.violations,
                reason=f"probability moved {decision.prior:.3f} -> {decision.posterior:.3f}",
            )
        )


update_graph = Graph(
    nodes=[CheckResolved, ApplyBayes, GuardUpdate, VerifyLargeMove],
    state_type=UpdateState,
    run_end_type=UpdateOutcome,
)


async def run_update_graph(forecast_id: str, *, verbose: bool = False) -> UpdateOutcome:
    """Run the daily cycle on one forecast. Replaces refresh_forecast + check_resolution.

    Callable from cron, the API, or the CLI — the trigger is not the graph's business.
    """
    record = db.get_forecast(forecast_id)
    if record is None:
        return UpdateOutcome(reason="forecast not found")
    if record.outcome is not None or record.is_ambiguous:
        return UpdateOutcome(reason="forecast already resolved")

    deps = ForecastDeps(verbose=verbose)
    state = UpdateState(record=record)
    result = await update_graph.run(CheckResolved(), state=state, deps=deps)
    return result.output


def update_mermaid() -> str:
    """The real graph as mermaid."""
    return update_graph.mermaid_code(start_node=CheckResolved)
