"""Runtime dependencies injected into every agent run.

Its own module rather than part of `graphs.state` because `tools` needs it and
`graphs` imports `agents` which imports `tools` — defining it alongside the graph
state would be a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .models import SourceRef


@dataclass
class SearchBudget:
    """One cell's search budget, and the column tag every event that cell produces carries.

    A *cell* is one column of the grid at one stage — `sc2`'s base-rate research, say.
    Decompose fixes the columns; each research stage runs one agent per column
    concurrently, and each of those agents gets its own budget rather than sharing one
    across the whole row.

    Two thresholds, not one. `soft_depth` — the cline — is where the agent starts being
    pushed to stop searching and commit; `hard_depth` is `UsageLimits.tool_calls_limit`
    and is a wall. The gap between them exists because a wall on its own gives the model
    no warning: it searches at full tilt and then dies mid-thought.

    `used` is incremented by the tools rather than read off `RunContext.usage`, because a
    tool needs the count at *return* time and `usage.tool_calls` is only incremented
    afterwards. One counter with one owner beats two that can drift.
    """

    sub_claim: str | None = None
    soft_depth: int = 0
    hard_depth: int = 0
    used: int = 0
    exhausted: bool = False

    @property
    def past_the_cline(self) -> bool:
        return self.soft_depth > 0 and self.used >= self.soft_depth

    @property
    def left(self) -> int:
        return max(0, self.hard_depth - self.used)


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
    """Every source a tool recorded, in the order the tools recorded it.

    A cell gets a *private* list, merged back into the parent's after the barrier. Not a
    style choice: `observability` detects new sources by remembering how long this list
    was and slicing off the tail, and two cells appending to one list makes that index
    hand each cell the other's sources.
    """

    emit: Callable[[str, dict[str, Any], str | None], None] | None = None
    """Fire-and-forget sink for live run events. None everywhere except a streamed run.

    Rides here rather than through `run_agent`'s signature because `deps` is already
    forwarded into `agent.run`, so the event stream handler can reach it from
    `ctx.deps` — threading it explicitly would mean editing all eight `run_<agent>`
    call sites to pass something none of them care about.

    Called as `emit(type, payload, sub_claim)`. The third argument is the column the
    event belongs to, or None for work a stage did as a whole.

    MUST be synchronous and non-blocking: it is called from inside the agent's own
    event stream, and awaiting there would stall token delivery.
    """

    budget: SearchBudget | None = None
    """The cell this deps copy is bound to, when a stage fans out across columns.

    None when nothing fanned out — the CLI, the update graph, an eval. A cell gets its
    own via `dataclasses.replace(deps, budget=…, sources_seen=[])`; the private
    `sources_seen` is not incidental, see that field.
    """

    @property
    def sub_claim(self) -> str | None:
        """Which column this deps copy is researching, if any."""
        return self.budget.sub_claim if self.budget else None

    @property
    def leaked_sources(self) -> list[SourceRef]:
        """Sources dated after `as_of`. Should always be empty — a non-empty list
        means the tool clamp has a bug, not that the forecast is merely suspect."""
        return [s for s in self.sources_seen if s.is_leak]
