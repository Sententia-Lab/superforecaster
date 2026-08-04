"""Runtime dependencies injected into every agent run.

Its own module rather than part of `graphs.state` because `tools` needs it and
`graphs` imports `agents` which imports `tools` — defining it alongside the graph
state would be a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import SourceRef


@dataclass
class ForecastDeps:
    """The two contamination clamps, plus the audit trail that proves they worked.

    In production both clamps are off: `as_of` is None so tools return current
    results, and `model` is None so agents use `resolve_agent_model()`.

    In a backtest both are set from the question's `asked_at`, so the agent can
    neither read a source published after the question was asked nor run on a model
    that was trained on the answer.
    """

    as_of: datetime | None = None
    model: str | None = None
    verbose: bool = False
    sources_seen: list[SourceRef] = field(default_factory=list)

    @property
    def leaked_sources(self) -> list[SourceRef]:
        """Sources dated after `as_of`. Should always be empty — a non-empty list
        means the tool clamp has a bug, not that the forecast is merely suspect."""
        return [s for s in self.sources_seen if s.is_leak]
