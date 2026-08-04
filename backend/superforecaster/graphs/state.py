"""State carried through the graphs.

`ForecastDeps` lives in `superforecaster.deps` rather than here, because `tools`
needs it and `graphs` imports `agents` which imports `tools`. It is re-exported so
callers have one obvious import site.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..deps import ForecastDeps
from ..models import (
    CheckViolation,
    Decomposition,
    Forecast,
    ForecastInput,
    ForecastRecord,
    InsideView,
    OutsideView,
    ResolutionCheckResult,
    UpdateDecision,
)

__all__ = ["ForecastDeps", "ForecastState", "UpdateState"]


@dataclass
class ForecastState:
    """Mutated as the forecast graph walks. Each node writes exactly one field.

    The fields being `None` until their node runs is what makes the ordering
    inspectable: `inside` cannot exist before `outside` does, because the node that
    writes it takes the other as input.
    """

    input: ForecastInput
    decomposition: Decomposition | None = None
    outside: OutsideView | None = None
    inside: InsideView | None = None
    forecast: Forecast | None = None
    violations: list[CheckViolation] = field(default_factory=list)
    synthesis_attempts: int = 0


@dataclass
class UpdateState:
    """Mutated as the update graph walks."""

    record: ForecastRecord
    resolution: ResolutionCheckResult | None = None
    decision: UpdateDecision | None = None
    violations: list[CheckViolation] = field(default_factory=list)
    verify_attempts: int = 0
