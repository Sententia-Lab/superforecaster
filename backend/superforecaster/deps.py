"""Runtime dependencies injected into every agent run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .config import Budget
from .events import Sink
from .models import ResearchDoc, ResearchHit, SourceRef


class ResearchStore(Protocol):
    """The pages this run has already read. `app.research` implements it over SQLite."""

    def remember(self, docs: list[ResearchDoc]) -> int: ...

    def find(self, query: str, limit: int = 5) -> list[ResearchHit]: ...


@dataclass
class ForecastDeps:
    sources_seen: list[SourceRef] = field(default_factory=list)
    """Every source a tool recorded. Each research cell gets a private list, because
    `runner` detects new sources by slicing the tail off it."""

    emit: Sink | None = None
    """Sink for live run events. Must be synchronous: it is called inside the agent's
    event stream."""

    sub_question: str | None = None
    """Which sub-question this deps copy is researching, or None for whole-run work."""

    store: ResearchStore | None = None
    """The run's research store, shared by every cell. None keeps nothing."""

    budget: Budget | None = None
    """Set by `runner.run_agent`; read by `agents.withdraw_tools` on every request."""
