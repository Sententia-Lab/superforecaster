"""Tests for graph checkpointing and resume.

The claim being verified is the expensive one: after a failure, resuming re-runs
exactly the agent that died and no other. A resume that quietly re-ran the whole graph
would look identical from the outside and cost five agent invocations to find out.
"""

from __future__ import annotations

import pytest

from superforecaster import checkpoints, runs
from superforecaster.graphs import forecast as fg
from tests.test_graph_forecast import (
    a_forecast,
    forecast_input,
    stub_agents,  # noqa: F401 — pytest fixture
)


@pytest.fixture(autouse=True)
def _clean_registry():
    runs.registry.clear()
    yield
    runs.registry.clear()


def failing(monkeypatch, step: str, exc: Exception):
    """Make one graph step raise, once."""
    calls = {"n": 0}

    async def boom(*a, **kw):
        calls["n"] += 1
        raise exc

    monkeypatch.setattr(fg, step, boom)
    return calls


# ---------- the checkpoint file ----------


async def test_a_successful_run_leaves_no_checkpoint(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    await run.task

    assert run.status == "done"
    assert not checkpoints.has_checkpoint(run.id)


async def test_a_failed_run_keeps_its_checkpoint(monkeypatch, stub_agents):  # noqa: F811
    failing(monkeypatch, "run_inside_view", RuntimeError("provider exploded"))
    run = runs.start(forecast_input())
    await run.task

    assert run.status == "error"
    assert checkpoints.has_checkpoint(run.id)


async def test_the_checkpoint_records_what_already_succeeded(
    monkeypatch, stub_agents  # noqa: F811
):
    failing(monkeypatch, "run_inside_view", RuntimeError("provider exploded"))
    run = runs.start(forecast_input())
    await run.task

    assert checkpoints.completed_stages(run.id) == ["Decompose", "FindBaseRates"]


async def test_the_error_event_names_what_survived(monkeypatch, stub_agents):  # noqa: F811
    """The offer to resume has to be concrete, not a button the user must trust."""
    failing(monkeypatch, "run_inside_view", RuntimeError("provider exploded"))
    run = runs.start(forecast_input())
    await run.task

    error = [e for e in run.events if e.type == "error"][0].payload
    assert error["resumable"] is True
    assert error["completed_stages"] == ["decompose", "outside"]


# ---------- rewinding ----------


async def test_rewind_names_the_node_that_will_re_run(monkeypatch, stub_agents):  # noqa: F811
    failing(monkeypatch, "run_inside_view", RuntimeError("provider exploded"))
    run = runs.start(forecast_input())
    await run.task

    assert checkpoints.rewind_for_resume(run.id) == "AdjustInsideView"


def test_rewind_on_a_missing_checkpoint_is_none():
    assert checkpoints.rewind_for_resume("run_never_existed") is None


# ---------- resume ----------


async def test_resume_re_runs_only_the_failed_node(monkeypatch, stub_agents):  # noqa: F811
    """The whole point. Decompose and FindBaseRates are not paid for twice."""
    calls = failing(monkeypatch, "run_inside_view", RuntimeError("provider exploded"))
    run = runs.start(forecast_input())
    await run.task
    assert run.status == "error"
    assert stub_agents["decompose"] == 1
    assert stub_agents["outside"] == 1

    # Repair whatever was wrong and pick up where it left off.
    monkeypatch.setattr(fg, "run_inside_view", _working_inside)
    resumed = runs.resume_run(run.id)
    await resumed.task

    assert resumed.status == "done"
    assert calls["n"] == 1  # the failing version ran once and was not retried
    assert stub_agents["decompose"] == 1  # <- not re-run
    assert stub_agents["outside"] == 1  # <- not re-run
    assert stub_agents["synthesize"] == 1


async def _working_inside(input, decomposition, outside, deps):
    from tests.test_graph_forecast import an_inside_view

    return an_inside_view()


async def test_resume_continues_the_same_event_stream(monkeypatch, stub_agents):  # noqa: F811
    """A watcher reconnecting with `?from_seq=` should see more of the same run, not a
    second run that happens to share an id."""
    failing(monkeypatch, "run_inside_view", RuntimeError("provider exploded"))
    run = runs.start(forecast_input())
    await run.task
    seq_at_failure = run.seq

    monkeypatch.setattr(fg, "run_inside_view", _working_inside)
    resumed = runs.resume_run(run.id)
    await resumed.task

    assert resumed.seq > seq_at_failure
    assert [e.seq for e in resumed.events] == sorted(e.seq for e in resumed.events)
    resume_event = [e for e in resumed.events if e.type == "resume"][0]
    assert resume_event.payload["from_node"] == "inside"
    assert resume_event.seq > seq_at_failure


async def test_resume_can_raise_the_search_budget(monkeypatch, stub_agents):  # noqa: F811
    """The usual reason to resume is that the budget ran out — resuming with the same
    one would walk into the same wall."""
    failing(monkeypatch, "run_inside_view", RuntimeError("nope"))
    run = runs.start(forecast_input())
    await run.task
    assert run.input.max_iterations == 5

    monkeypatch.setattr(fg, "run_inside_view", _working_inside)
    resumed = runs.resume_run(run.id, max_iterations=12)
    await resumed.task

    assert resumed.input.max_iterations == 12
    assert resumed.status == "done"


async def test_the_raised_budget_reaches_the_agent_that_failed(monkeypatch, stub_agents):  # noqa: F811
    """The regression: resuming deeper used to change nothing the agent could see.

    Research agents read the budget off graph state (`ctx.state.input`), and
    `iter_from_persistence` restores that from the snapshot — so a resumed node re-ran
    on the OLD depth and hit the same UsageLimitExceeded, while the UI reported the new
    one. `resumed.input.max_iterations` was 12 and the agent still saw 5.
    """
    seen: list[int] = []

    async def recording_inside(input, decomposition, outside, deps):
        seen.append(input.max_iterations)
        from tests.test_graph_forecast import an_inside_view

        return an_inside_view()

    failing(monkeypatch, "run_inside_view", RuntimeError("nope"))
    run = runs.start(forecast_input())
    await run.task

    monkeypatch.setattr(fg, "run_inside_view", recording_inside)
    resumed = runs.resume_run(run.id, max_iterations=12)
    await resumed.task

    assert seen == [12], f"the resumed agent ran at depth {seen}, not 12"


async def test_run_summary_reports_the_depth_in_use(monkeypatch, stub_agents):  # noqa: F811
    """The resume prompt prefills from this; without it, it always suggested 10."""
    failing(monkeypatch, "run_inside_view", RuntimeError("nope"))
    run = runs.start(forecast_input())
    await run.task
    assert run.summary().max_iterations == 5

    monkeypatch.setattr(fg, "run_inside_view", _working_inside)
    resumed = runs.resume_run(run.id, max_iterations=12)
    await resumed.task
    assert resumed.summary().max_iterations == 12


async def test_resume_produces_a_saved_forecast(monkeypatch, stub_agents):  # noqa: F811
    from superforecaster import db

    failing(monkeypatch, "run_synthesize", RuntimeError("provider exploded"))
    run = runs.start(forecast_input())
    await run.task

    async def working_synth(input, d, o, i, violations, deps):
        return a_forecast(0.28)

    monkeypatch.setattr(fg, "run_synthesize", working_synth)
    resumed = runs.resume_run(run.id)
    await resumed.task

    assert resumed.forecast_id is not None
    assert db.get_forecast(resumed.forecast_id) is not None
    assert db.get_run(run.id)["status"] == "done"
    # The checkpoint is gone — nothing can need it once the forecast is saved.
    assert not checkpoints.has_checkpoint(run.id)


async def test_resuming_a_finished_run_is_refused(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    await run.task

    with pytest.raises(LookupError):
        runs.resume_run(run.id)


async def test_resuming_a_live_run_is_refused(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    try:
        with pytest.raises(ValueError):
            runs.resume_run(run.id)
    finally:
        await run.task


def test_resuming_an_unknown_run_is_refused():
    with pytest.raises(LookupError):
        runs.resume_run("run_never_existed")
