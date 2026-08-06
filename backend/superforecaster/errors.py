"""Failures this system raises on purpose.

Its own module because the timeouts are raised in `observability` and `machine` and
caught in `agents.critic`, `api.questions`, and `api.runs` — a shared home keeps that
from becoming an import cycle between the agent layer and the run layer.

Both subclass `TimeoutError` so a caller that only cares that something ran out of time
can catch one thing, and both are `Exception`s so `machine.execute_step`'s catch-all
still lands them as the step's `error` text.
"""

from __future__ import annotations


class AgentTimeout(TimeoutError):
    """One agent run exceeded `AGENT_TIMEOUT_SECONDS`.

    Distinct from `UsageLimitExceeded`, which means the agent acted too many times. This
    one means it stopped acting: a provider request that never returned, or a stream that
    went quiet mid-token. No limit is ever reached on that path and no exception is ever
    raised, so without this the run simply never ends.
    """


class StageTimeout(TimeoutError):
    """One gated stage step exceeded `STAGE_TIMEOUT_SECONDS`.

    The backstop above `AgentTimeout`: a handful of agent calls that each land just
    inside their own ceiling still add up to a step nobody is waiting for. There is
    deliberately no whole-run timeout — a gated run sits idle at a gate indefinitely;
    only the work between two clicks is bounded.
    """
