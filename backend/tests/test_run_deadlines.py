"""Every way a run can stop, and the proof that each one still closes the stream.

The failure this file exists for is not a wrong answer — it is a run that never ends. A
provider request that hangs reaches no usage limit, raises nothing, and leaves the
browser on a loading state waiting for an `end` frame nobody will send. Three ceilings
close that hole, and each one has to produce a terminal frame rather than silence:

    AGENT_TIMEOUT_SECONDS       one agent stopped responding
    RUN_TIMEOUT_SECONDS         the run as a whole outlived its budget
    RUN_WATCHER_GRACE_SECONDS   every client went away and none came back

The last one is not a failure at all. It is the answer to "the tab is closed and the
agents are still spending my API key."
"""

from __future__ import annotations

import asyncio

import pytest

from superforecaster import runs
from superforecaster.errors import AgentTimeout, RunTimeout
from superforecaster.models import ForecastInput
from superforecaster.observability import run_agent
from tests.test_graph_forecast import forecast_input


@pytest.fixture(autouse=True)
def _clean_registry():
    runs.registry.clear()
    yield
    runs.registry.clear()


def _terminal_frames(run: runs.Run) -> list[str]:
    return [e.type for e in run.events][-2:]


# ---------- one agent stops responding ----------


async def test_a_hanging_agent_raises_agent_timeout_rather_than_hanging(monkeypatch):
    """The core hole. `agent.run` never returns; without a deadline neither does the run."""

    class Hanging:
        async def run(self, *a, **kw):
            await asyncio.sleep(3600)

    monkeypatch.setenv("AGENT_TIMEOUT_SECONDS", "0.05")

    with pytest.raises(AgentTimeout) as exc:
        await run_agent(Hanging(), "prompt", run_name="stuck step")

    assert "stuck step" in str(exc.value)


async def test_the_deadline_can_be_switched_off(monkeypatch):
    """Zero disables it — the escape hatch for a long backtest, and the reason the
    timeout is a helper rather than an inline `async with asyncio.timeout(...)`."""

    class Quick:
        async def run(self, *a, **kw):
            return _AgentResult()

    monkeypatch.setenv("AGENT_TIMEOUT_SECONDS", "0")
    result = await run_agent(Quick(), "prompt", run_name="fine")
    assert result.output == "ok"


class _AgentResult:
    output = "ok"

    def usage(self):
        class U:
            requests = 1
            tool_calls = 0
            total_tokens = 10

        return U()


# ---------- the run as a whole ----------


async def test_a_run_past_its_deadline_ends_error_not_silence(monkeypatch):
    """A stalled run must still emit `error` then `end`, because the SSE generator
    returns on `end` and nothing else. No frame means a socket held open forever."""
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("RUN_WATCHER_GRACE_SECONDS", "0")

    async def crawl(run_id):
        await asyncio.sleep(3600)

    monkeypatch.setattr(runs, "_run_forecast", crawl)

    run = runs.registry.create(forecast_input())
    await runs.execute(run)

    assert run.status == "error"
    assert "RunTimeout" in (run.error or "")
    assert _terminal_frames(run) == ["error", "end"]


async def test_the_run_timeout_hint_says_what_to_change(monkeypatch):
    hint = runs._failure_hint(RunTimeout("too slow"))
    assert "RUN_TIMEOUT_SECONDS" in hint

    hint = runs._failure_hint(AgentTimeout("stopped responding"))
    assert "AGENT_TIMEOUT_SECONDS" in hint


# ---------- nobody is watching ----------


async def test_a_run_nobody_watches_is_cancelled(monkeypatch):
    """The tab-closed case. A run is thirty-odd agent calls against a live search
    budget; finishing it for a closed tab spends the budget and shows nobody."""
    monkeypatch.setenv("RUN_WATCHER_GRACE_SECONDS", "0.06")
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "0")

    started = asyncio.Event()

    async def crawl(run_id):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(runs, "_run_forecast", crawl)

    run = runs.start(forecast_input())
    await started.wait()
    with pytest.raises(asyncio.CancelledError):
        await run.task

    assert run.status == "cancelled"
    assert run.abandoned is True
    assert _terminal_frames(run) == ["error", "end"]
    assert "No client watched" in run.events[-2].payload["message"]


async def test_a_watched_run_is_left_alone(monkeypatch):
    """The grace window has to survive an actual subscriber, or the feature is just a
    second run timeout with a confusing name."""
    monkeypatch.setenv("RUN_WATCHER_GRACE_SECONDS", "0.06")
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "0")

    async def crawl(run_id):
        await asyncio.sleep(0.4)

    monkeypatch.setattr(runs, "_run_forecast", crawl)

    run = runs.start(forecast_input())
    queue = run.subscribe()  # a browser with the stream open
    try:
        await run.task
    finally:
        run.unsubscribe(queue)

    assert run.status == "done"
    assert run.abandoned is False


async def test_the_watchdog_is_torn_down_with_the_run(monkeypatch):
    """A finished run must not leave a sleeping task behind — a pending task at loop
    shutdown is a warning at best and a hang at worst."""
    monkeypatch.setenv("RUN_WATCHER_GRACE_SECONDS", "30")
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "0")

    async def quick(run_id):
        return None

    monkeypatch.setattr(runs, "_run_forecast", quick)

    run = runs.start(forecast_input())
    await run.task
    await asyncio.sleep(0)  # let the done callback fire

    assert run.watchdog is None


async def test_the_watchdog_can_be_disabled(monkeypatch):
    """`RUN_WATCHER_GRACE_SECONDS=0` is fire-and-forget, for a deployment that wants it."""
    monkeypatch.setenv("RUN_WATCHER_GRACE_SECONDS", "0")
    monkeypatch.setenv("RUN_TIMEOUT_SECONDS", "0")

    async def crawl(run_id):
        await asyncio.sleep(0.15)

    monkeypatch.setattr(runs, "_run_forecast", crawl)

    run = runs.start(forecast_input())
    await run.task

    assert run.status == "done"
    assert run.abandoned is False


# ---------- the input ----------


def test_a_run_needs_somebody_to_adjudicate_it():
    """A forecast with no resolution source cannot be scored, and the gap is invisible
    until the one day it is too late to fix."""
    from pydantic import ValidationError

    from superforecaster.models import CreateRunRequest

    base = {
        "question": "Will X happen?",
        "resolution_criteria": "X is observable.",
        "resolution_date": forecast_input().resolution_date,
    }

    with pytest.raises(ValidationError):
        CreateRunRequest(**base)
    with pytest.raises(ValidationError):
        CreateRunRequest(**base, resolution_source="")

    assert CreateRunRequest(**base, resolution_source="ONS bulletin").resolution_source


def test_forecast_input_is_unchanged():
    """`resolution_source` is a property of the run, not of what the agents are asked.
    Requiring it on the request must not leak into the graph's own input type."""
    assert "resolution_source" not in ForecastInput.model_fields
