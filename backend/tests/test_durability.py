"""Tests for the durable path — the one the server actually takes.

These exist because the first version of this feature shipped broken and the suite did
not notice: `durability.configure()` is only called from the API lifespan, so every
other test runs with `is_active() == False` and takes the plain, un-checkpointed branch.
The claim being made here — "a failed run resumes without re-paying for the research" —
was true of nothing, and no test could have said so.

Each test drives a whole run with stubbed agents and **counts real invocations**. That
is the only assertion that can tell resuming from re-running: a workflow that replays
its recorded result and a workflow that re-executes look identical from the outside.

One event loop for the whole module, rather than an async test or a bare `asyncio.run`
per test. DBOS binds a thread pool to the loop that launched it, and both alternatives
close their loop when they finish — the next test then dies with "cannot schedule new
futures after shutdown". The loop is created alongside DBOS and outlives every test in
the file.
"""

from __future__ import annotations

import asyncio

import pytest

from superforecaster import durability, runs
from superforecaster.graphs import forecast as fg
from superforecaster.models import (
    Reflection,
    SubClaimAdjustments,
    SubClaimBaseRates,
    SubClaimLenses,
)
from tests.test_checks import adjustment, all_bias_checks, ref
from tests.test_graph_forecast import a_decomposition, a_forecast, a_lens, forecast_input


_LOOP: asyncio.AbstractEventLoop | None = None


@pytest.fixture(scope="module", autouse=True)
def _dbos(tmp_path_factory):
    """One DBOS and one event loop for the module. Both are process-global in effect."""
    global _LOOP
    tmp = tmp_path_factory.mktemp("durable")
    import os

    os.environ["DBOS_DATABASE_URL"] = f"sqlite:///{tmp}/dbos.sqlite"
    os.environ["DATABASE_PATH"] = str(tmp / "app.db")

    _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)

    from superforecaster import db

    db.init_db()
    durability.configure()
    yield
    # Both are process-global. Leaving either in place makes every test that runs after
    # this file take the durable branch against a closed loop.
    durability.shutdown()
    _LOOP.close()
    _LOOP = None


@pytest.fixture(autouse=True)
def _clean_registry():
    runs.registry.clear()
    yield
    runs.registry.clear()


class Counting:
    """Stubs for every agent, counting how many times each actually executed."""

    def __init__(self, fail_synth_times: int = 0) -> None:
        self.calls = {"decompose": 0, "lenses": 0, "base_rate": 0, "inside": 0, "reflect": 0, "synth": 0}
        self._fails_left = fail_synth_times

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(fg, "run_decompose", self.decompose)
        monkeypatch.setattr(fg, "run_choose_lenses", self.choose)
        monkeypatch.setattr(fg, "run_research_lens", self.base_rate)
        monkeypatch.setattr(fg, "run_adjust_lens", self.inside)
        monkeypatch.setattr(fg, "run_reflect", self.reflect)
        monkeypatch.setattr(fg, "run_synthesize", self.synth)

    async def decompose(self, input, deps):
        self.calls["decompose"] += 1
        return a_decomposition()

    async def choose(self, input, decomposition, sub_claim, deps):
        self.calls["lenses"] += 1
        return SubClaimLenses(lenses=[a_lens(f"{sub_claim.id}-lens")])

    async def base_rate(self, input, sub_claim, lens, deps):
        self.calls["base_rate"] += 1
        return SubClaimBaseRates(lens=ref(lens.name, 0.22), disagreement="")

    async def inside(self, input, sub_claim, lens, already_controlled_for, deps):
        self.calls["inside"] += 1
        moves = (
            [adjustment("up", 0.10), adjustment("down", 0.04)]
            if sub_claim.id == "sc1"
            else [adjustment("neutral", 0.0, is_noise=True)]
        )
        return SubClaimAdjustments(
            lens_name=lens.name,
            adjustments=moves,
            steel_man="x",
            what_would_change_my_mind="y",
        )

    async def reflect(self, input, d, o, adjustments, steel_mans, deps):
        self.calls["reflect"] += 1
        return Reflection(
            steel_man="x", what_would_change_my_mind="y", bias_checks=all_bias_checks()
        )

    async def synth(self, input, d, o, i, violations, deps):
        self.calls["synth"] += 1
        if self._fails_left > 0:
            self._fails_left -= 1
            raise RuntimeError("provider exploded")
        return a_forecast()


def _run(coro):
    """Drive a coroutine on the module's loop. See the module docstring for why."""
    assert _LOOP is not None
    return _LOOP.run_until_complete(coro)


def test_durability_is_active_in_these_tests():
    """The guard on the guard. If this ever goes false the rest of the file is vacuous —
    it would be exercising the same un-checkpointed path everything else already does."""
    assert durability.is_active() is True


def test_a_clean_run_completes_under_the_workflow(monkeypatch):
    agents = Counting()
    agents.install(monkeypatch)

    async def go():
        run = runs.start(forecast_input())
        await run.task
        return run

    run = _run(go())
    assert run.status == "done", run.error
    assert agents.calls["decompose"] == 1
    assert agents.calls["synth"] == 1


def test_resume_does_not_re_pay_for_completed_research(monkeypatch):
    """The whole point of the feature.

    Synthesis fails once, then succeeds. Resuming must re-run synthesis and *nothing
    before it* — the decomposition and both research rows were already paid for.
    """
    agents = Counting(fail_synth_times=1)
    agents.install(monkeypatch)

    async def go():
        run = runs.start(forecast_input())
        await run.task
        assert run.status == "error", "the first attempt was supposed to fail"
        before = dict(agents.calls)

        resumed = runs.resume_run(run.id)
        await resumed.task
        return resumed, before

    resumed, before = _run(go())

    assert resumed.status == "done", resumed.error
    redone = {k: agents.calls[k] - before[k] for k in agents.calls}

    # The research is the expensive part and it must not run twice.
    assert redone["decompose"] == 0
    assert redone["lenses"] == 0
    assert redone["base_rate"] == 0
    assert redone["inside"] == 0
    assert redone["reflect"] == 0
    # The step that failed is the one that re-runs.
    assert redone["synth"] == 1


def test_the_resumed_run_keeps_streaming_into_the_same_trail(monkeypatch):
    """`seq` keeps counting, so a client watching from `?from_seq=` sees the resume as
    more of the same run rather than a second one."""
    agents = Counting(fail_synth_times=1)
    agents.install(monkeypatch)

    async def go():
        run = runs.start(forecast_input())
        await run.task
        seq_after_failure = run.seq
        resumed = runs.resume_run(run.id)
        await resumed.task
        return run, seq_after_failure

    run, seq_after_failure = _run(go())

    assert run.seq > seq_after_failure
    assert [e.type for e in run.events][-2:] == ["result", "end"]
    assert any(e.type == "resume" for e in run.events)
