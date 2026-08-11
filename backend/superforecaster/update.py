"""The daily update cycle — four nodes, one verification loop, and no storage.

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

The nodes used to write to SQLite as they ran — `mark_refreshed` inside `CheckResolved`,
`add_forecast_update` inside `GuardUpdate` — and `run_update_graph` looked the record up
itself, so "forecast not found" was a storage answer coming back as a forecasting
outcome. Now the cycle takes a record and returns an `UpdateOutcome` describing what
should happen. `app.update` is the caller that owns the database and performs the write.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_graph import BaseNode, End, GraphBuilder, GraphRunContext

from . import checks
from .agents.resolution import run_resolution_check
from .agents.update import run_update
from .config import get_check_thresholds
from .deps import ForecastDeps
from .models import (
    CheckViolation,
    ForecastRecord,
    ResolutionCheckResult,
    UpdateDecision,
    UpdateOutcome,
)

MAX_VERIFY_ATTEMPTS = 1


@dataclass
class UpdateState:
    """Mutated as the update cycle walks."""

    record: ForecastRecord
    resolution: ResolutionCheckResult | None = None
    decision: UpdateDecision | None = None
    violations: list[CheckViolation] = field(default_factory=list)
    verify_attempts: int = 0


@dataclass
class CheckResolved(BaseNode[UpdateState, ForecastDeps, UpdateOutcome]):
    """Has this already resolved? If so, nothing else may touch it."""

    async def run(
        self, ctx: GraphRunContext[UpdateState, ForecastDeps]
    ) -> ApplyBayes | End[UpdateOutcome]:
        result = await run_resolution_check(ctx.state.record, ctx.deps)
        ctx.state.resolution = result

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

        return End(
            UpdateOutcome(
                updated=True,
                new_probability=decision.posterior,
                violations=ctx.state.violations,
                reason=f"probability moved {decision.prior:.3f} -> {decision.posterior:.3f}",
                reasoning=decision.reasoning,
            )
        )


def _build_graph():
    """Assemble the four nodes into a graph.

    The edges are not declared here. Each node's `run` return type states which nodes
    may follow it, and `builder.node` reads that hint — so `GuardUpdate -> VerifyLargeMove`
    exists because `GuardUpdate.run` returns `VerifyLargeMove | End[UpdateOutcome]`, and
    nowhere else. The only edge written by hand is the one into the first node.
    """
    builder = GraphBuilder(
        name="update",
        state_type=UpdateState,
        deps_type=ForecastDeps,
        input_type=CheckResolved,
        output_type=UpdateOutcome,
    )
    builder.add(
        builder.edge_from(builder.start_node).to(CheckResolved),
        builder.node(CheckResolved),
        builder.node(ApplyBayes),
        builder.node(GuardUpdate),
        builder.node(VerifyLargeMove),
    )
    return builder.build()


update_graph = _build_graph()


async def run_update_cycle(
    record: ForecastRecord, deps: ForecastDeps | None = None
) -> UpdateOutcome:
    """Check one forecast for resolution, then update it against new evidence.

    Reads nothing and writes nothing. The caller supplies the record and decides what
    to do with the outcome.
    """
    return await update_graph.run(
        inputs=CheckResolved(),
        state=UpdateState(record=record),
        deps=deps or ForecastDeps(),
    )


def update_mermaid() -> str:
    """The real graph as mermaid."""
    return update_graph.render()
