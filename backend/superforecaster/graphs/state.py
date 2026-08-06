"""State carried through the update graph.

`ForecastDeps` lives in `superforecaster.deps` rather than here, because `tools`
needs it and `graphs` imports `agents` which imports `tools`. It is re-exported so
callers have one obvious import site.

The forecast pipeline no longer carries graph state — its state is the `run_steps`
table (ADR 45).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..deps import ForecastDeps
from ..models import (
    CheckViolation,
    ForecastRecord,
    ResolutionCheckResult,
    UpdateDecision,
)

__all__ = ["ForecastDeps", "UpdateState"]


@dataclass
class UpdateState:
    """Mutated as the update graph walks."""

    record: ForecastRecord
    resolution: ResolutionCheckResult | None = None
    decision: UpdateDecision | None = None
    violations: list[CheckViolation] = field(default_factory=list)
    verify_attempts: int = 0
