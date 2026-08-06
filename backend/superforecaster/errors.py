"""Failures this system raises on purpose.

Its own module because the two timeouts are raised in `observability` and `runs` and
caught in `agents.critic`, `api.questions`, and `runs` — a shared home keeps that from
becoming an import cycle between the agent layer and the run layer.

Both subclass `TimeoutError` so a caller that only cares that something ran out of time
can catch one thing, and both are `Exception`s so `runs.execute`'s catch-all still turns
them into an `error` frame followed by an `end` frame.
"""

from __future__ import annotations


class AgentTimeout(TimeoutError):
    """One agent run exceeded `AGENT_TIMEOUT_SECONDS`.

    Distinct from `UsageLimitExceeded`, which means the agent acted too many times. This
    one means it stopped acting: a provider request that never returned, or a stream that
    went quiet mid-token. No limit is ever reached on that path and no exception is ever
    raised, so without this the run simply never ends.
    """


class RunTimeout(TimeoutError):
    """A whole forecast run exceeded `RUN_TIMEOUT_SECONDS`.

    The backstop above `AgentTimeout`: thirty-odd agent calls that each land just inside
    their own ceiling still add up to a run nobody is waiting for.
    """


class RunAbandoned(RuntimeError):
    """Every client watching this run went away and none came back.

    Not an error in the run — the reason it was stopped. A run is a live search budget
    charged to somebody's API key, and continuing to spend it for a closed tab is the
    single largest source of wasted work here.
    """
