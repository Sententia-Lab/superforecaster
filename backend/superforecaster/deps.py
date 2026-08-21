"""Runtime dependencies injected into every agent run.

Its own module rather than part of `update` because `tools` needs it and `update`
imports `agents` which imports `tools` — defining it beside the update state would be
a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .config import Budget

from .events import Sink
from .models import ResearchDoc, ResearchHit, SourceRef


class ResearchStore(Protocol):
    """The pages this run has already read, bound to the run that read them.

    A protocol rather than an import because the store is SQLite and this package is a
    library — `test_layering` keeps storage in `app`. The app layer passes an
    implementation in, the same way it passes a `Sink`.

    Already scoped to one run, so no caller passes an id around: two runs cannot see each
    other's pages because neither ever names the other's store.
    """

    def remember(self, docs: list[ResearchDoc]) -> int:
        """Keep these pages. Re-storing a URL updates it rather than adding a copy."""
        ...

    def find(self, query: str, limit: int = 5) -> list[ResearchHit]:
        """The stored pages ranked against `query`, best first."""
        ...

    def is_empty(self) -> bool:
        """Whether nothing has been stored yet."""
        ...


@dataclass
class ForecastDeps:
    """The two contamination clamps, plus the audit trail that proves they worked.

    In production both are off: `forecast_date` is None so `search_wikipedia` fetches the
    current article, and `model` is None so agents use `resolve_agent_model()`.

    In a backtest both are set from the question's `asked_at`, so the agent can
    neither read a source published after the question was asked nor run on a model
    that was trained on the answer.
    """

    forecast_date: datetime | None = None
    """The date the agent is forecasting from. `search_wikipedia` fetches the revision as
    of this date, `agents.forecast_date_note` tells the model it is in the past, and
    `model_garden.pick_clean_model` picks a model trained before it. The Tavily tools
    ignore it — ADR 17 records why their clamp was removed rather than repaired."""
    model: str | None = None
    sources_seen: list[SourceRef] = field(default_factory=list)
    """Every source a tool recorded, in the order the tools recorded it.

    A cell gets a *private* list, merged back into the parent's after the barrier. Not a
    style choice: `runner` detects new sources by remembering how long this list
    was and slicing off the tail, and two cells appending to one list makes that index
    hand each cell the other's sources.
    """

    emit: Sink | None = None
    """Fire-and-forget sink for live run events. None everywhere except a streamed run.

    Rides here rather than through `run_agent`'s signature because `deps` is already
    forwarded into `agent.run`, so the event stream handler can reach it from
    `ctx.deps` — threading it explicitly would mean editing all eight `run_<agent>`
    call sites to pass something none of them care about.

    Called as `emit(event, sub_question)` with an `events.AgentEvent`. The second
    argument is the column the event belongs to, or None for work a stage did as a whole.

    MUST be synchronous and non-blocking: it is called from inside the agent's own
    event stream, and awaiting there would stall token delivery.
    """

    sub_question: str | None = None
    """Which column of the grid this deps copy is researching, if any.

    A *cell* is one column at one stage — `sq2`'s base-rate research, say. Decompose
    fixes the columns; each research stage runs one agent per column concurrently, and
    each of those agents gets a deps copy carrying its own tag and its own
    `sources_seen`. None when nothing fanned out — the CLI, the update graph, an eval.
    """

    store: ResearchStore | None = None
    """The pages this run has already read, or None to keep none.

    One store per run, copied unchanged into every cell, because the whole point is that a
    later stage can read what an earlier one fetched. `sources_seen` is private per cell
    for the opposite reason — it is an audit trail of one cell's work, and this is a
    shared record of the run's.

    Rides here rather than through each tool's signature for the same reason `emit` does:
    `deps` is already forwarded into `agent.run`, so a tool reaches it from `ctx.deps`.

    None everywhere it is not set: `search_research` is withdrawn and the Tavily tools
    skip their write, so a direct call or a test without one behaves as it always did.
    """

    budget: Budget | None = None
    """What this run may spend. `observability.run_agent` puts it here.

    It rides on deps rather than staying a `run_agent` local because the budget
    instruction reads it from `ctx.deps` on every model request — which is what makes
    the remaining budget a live number rather than a sentence written once.
    """
