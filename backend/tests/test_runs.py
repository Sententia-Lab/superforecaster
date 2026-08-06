"""Tests for the live-run registry, its event buffer, and the state projections.

Nothing here starts an agent. The graph is driven with the same stubs
`test_graph_forecast.py` uses, so what is verified is that a methodology-clean run
produces the events a UI needs — and that a run which fails still says so.
"""

from __future__ import annotations

import asyncio

import pytest

from superforecaster import checks, runs
from superforecaster.graphs import forecast as fg
from superforecaster.graphs.state import ForecastState
from superforecaster.models import RunEvent
from tests.test_graph_forecast import (
    a_decomposition,
    a_forecast,
    an_inside_view,
    an_outside_view,
    forecast_input,
    stub_agents,  # noqa: F401 — pytest fixture
)


@pytest.fixture(autouse=True)
def _clean_registry():
    runs.registry.clear()
    yield
    runs.registry.clear()


def a_run(**kw) -> runs.Run:
    return runs.Run(id="run_test", input=forecast_input(), **kw)


def types_of(events) -> list[str]:
    return [e.type for e in events]


# ---------- the buffer ----------


def test_seq_is_monotonic_and_stamped_with_the_current_stage():
    run = a_run()
    run.stage, run.attempt = "outside", 1
    run.emit("query", {"tool": "search_web"})
    run.stage, run.attempt = "synth", 2
    run.emit("draft", {"p": 0.3})

    assert [e.seq for e in run.events] == [1, 2]
    assert [(e.stage, e.attempt) for e in run.events] == [("outside", 1), ("synth", 2)]


def test_thought_deltas_coalesce_into_one_event():
    run = a_run()
    for chunk in ("The ", "outside ", "view "):
        run.emit_thought(chunk)
    run.flush_thought()

    assert types_of(run.events) == ["thought"]
    assert run.events[0].payload["delta"] == "The outside view "


def test_pending_thought_flushes_before_any_other_event():
    """Narration must never arrive after the tool call it preceded."""
    run = a_run()
    run.emit_thought("I should search for ")
    run.emit("query", {"tool": "search_web"})

    assert types_of(run.events) == ["thought", "query"]


def test_two_columns_narrating_at_once_do_not_concatenate():
    """The whole reason `_thoughts` is keyed. One buffer would interleave these into
    "The sc1 A sc2 base rate" and hand the reader a sentence neither agent wrote."""
    run = a_run()
    run.emit_thought("The base rate ", "sc1")
    run.emit_thought("A reference class ", "sc2")
    run.emit_thought("for S-1 filings ", "sc1")
    run.flush_thought()

    by_column = {e.sub_claim: e.payload["delta"] for e in run.events}
    assert by_column == {
        "sc1": "The base rate for S-1 filings ",
        "sc2": "A reference class ",
    }


def test_an_event_flushes_only_its_own_column():
    """sc1's tool call must not drag sc2's half-written sentence out in front of it."""
    run = a_run()
    run.emit_thought("I should search for ", "sc1")
    run.emit_thought("this is only half a ", "sc2")
    run.emit("query", {"tool": "search_web"}, "sc1")

    assert types_of(run.events) == ["thought", "query"]
    assert [e.sub_claim for e in run.events] == ["sc1", "sc1"]

    run.flush_thought()
    assert run.events[-1].sub_claim == "sc2"


def test_an_untagged_event_does_not_flush_a_column():
    """None is a real column key, not a wildcard — it is what everything outside a
    fanned-out stage emits under."""
    run = a_run()
    run.emit_thought("mid-sentence ", "sc1")
    run.emit("note", {"label": "chain_note", "text": "x"})

    assert types_of(run.events) == ["note"]


def test_a_stage_boundary_flushes_every_column():
    run = a_run()
    run.emit_thought("a", "sc1")
    run.emit_thought("b", "sc2")
    run.flush_thought()

    assert types_of(run.events) == ["thought", "thought"]


def test_events_carry_no_column_by_default():
    """Every pre-3.3 caller keeps working, and old buffered events still deserialize."""
    run = a_run()
    run.emit("draft", {"p": 0.3})

    assert run.events[0].sub_claim is None


def test_ring_buffer_evicts_and_replay_reports_the_gap(monkeypatch):
    monkeypatch.setenv("RUN_EVENT_BUFFER", "3")
    run = a_run()
    for i in range(6):
        run.emit("note", {"i": i})

    assert len(run.events) == 3
    assert run.dropped == 3

    replayed = run.replay(from_seq=0)
    assert replayed[0].type == "truncated"
    assert replayed[0].payload["dropped_before_seq"] == 4


def test_replay_from_a_live_seq_has_no_truncation_marker(monkeypatch):
    monkeypatch.setenv("RUN_EVENT_BUFFER", "3")
    run = a_run()
    for i in range(6):
        run.emit("note", {"i": i})

    replayed = run.replay(from_seq=5)
    assert types_of(replayed) == ["note", "note"]


def test_subscriber_receives_events_and_unsubscribes_cleanly():
    run = a_run()
    q = run.subscribe()
    run.emit("stage", {"stage": "decompose"})

    assert q.get_nowait().type == "stage"
    run.unsubscribe(q)
    run.emit("stage", {"stage": "outside"})
    assert q.empty()


def test_a_full_subscriber_is_dropped_rather_than_stalling_the_run(monkeypatch):
    """A dead client costs its own connection, never the graph."""
    monkeypatch.setattr(runs, "SUBSCRIBER_QUEUE_SIZE", 2)
    run = a_run()
    run.subscribe()

    for i in range(5):
        run.emit("note", {"i": i})

    assert run.seq == 5  # every event still recorded
    assert run.stream._subscribers == set()  # the slow subscriber was dropped


# ---------- the registry ----------


def test_create_refuses_past_the_concurrency_cap(monkeypatch):
    monkeypatch.setenv("RUN_MAX_CONCURRENT", "2")
    runs.registry.create(forecast_input())
    runs.registry.create(forecast_input())

    with pytest.raises(runs.SlotsFullError):
        runs.registry.create(forecast_input())


def test_a_finished_run_frees_its_slot(monkeypatch):
    monkeypatch.setenv("RUN_MAX_CONCURRENT", "1")
    first = runs.registry.create(forecast_input())
    first.status = "done"

    assert runs.registry.slots_free() == 1
    runs.registry.create(forecast_input())  # does not raise


def test_create_writes_a_queued_db_row():
    run = runs.registry.create(forecast_input(), resolution_source="SEC EDGAR")
    from superforecaster import db

    row = db.get_run(run.id)
    assert row is not None
    assert row["status"] == "queued"
    assert row["resolution_source"] == "SEC EDGAR"


# ---------- driving a run ----------


async def test_a_full_run_emits_every_stage_and_ends_done(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    await run.task

    assert run.status == "done", run.error
    assert run.forecast_id is not None

    stages = [e.payload["stage"] for e in run.events if e.type == "stage"]
    assert stages == [
        "decompose", "lenses", "outside", "inside", "reflect", "synth", "critique"
    ]
    assert types_of(run.events)[-2:] == ["result", "end"]


async def test_stage_results_are_the_models_the_agents_returned(stub_agents):  # noqa: F811
    """The backend does no reshaping. A stage's result event is its typed object dumped,
    so the UI reads what the methodology produced rather than a second telling of it."""
    run = runs.start(forecast_input())
    await run.task

    by_type = {e.type: e.payload for e in run.events}

    assert [s["id"] for s in by_type["decompose"]["sub_claims"]] == ["sc1", "sc2", "sc3"]
    assert "lenses" in by_type["outside"]
    assert "aggregate_base_rate" in by_type["outside"]
    assert "adjustments" in by_type["inside"]
    assert len(by_type["inside"]["bias_checks"]) == 5
    assert by_type["synth"]["probability"] == pytest.approx(0.28)


async def test_the_result_event_carries_the_forecast_and_its_violations(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    await run.task

    result = [e for e in run.events if e.type == "result"][0].payload
    assert result["forecast_id"] == run.forecast_id
    assert result["forecast"]["probability"] == pytest.approx(0.28)
    assert result["violations"] == []


async def test_a_finished_run_is_recorded_in_the_db(stub_agents):  # noqa: F811
    from superforecaster import db

    run = runs.start(forecast_input())
    await run.task

    row = db.get_run(run.id)
    assert row["status"] == "done"
    assert row["forecast_id"] == run.forecast_id
    assert db.get_forecast(run.forecast_id) is not None


async def test_a_crashing_run_emits_error_then_end(monkeypatch, stub_agents):  # noqa: F811
    """A client cannot tell a hung server from a crashed one — so say which."""

    async def boom(*a, **kw):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(fg, "run_decompose", boom)

    run = runs.start(forecast_input())
    await run.task

    assert run.status == "error"
    assert "provider exploded" in run.error
    assert types_of(run.events)[-2:] == ["error", "end"]


async def test_a_failed_run_reports_whether_it_can_be_resumed(monkeypatch, stub_agents):  # noqa: F811
    """The offer to resume has to be real rather than a button the operator has to
    trust, so it reports whether this process is actually checkpointing. Tests run
    without durability, so it is False here and True under the server."""
    from superforecaster import durability

    async def boom(*a, **kw):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(fg, "run_synthesize", boom)

    run = runs.start(forecast_input())
    await run.task

    error = [e for e in run.events if e.type == "error"][0].payload
    assert error["resumable"] is durability.is_active()
    assert error["message"].startswith("RuntimeError")


async def test_a_cancelled_run_still_closes_its_stream(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    run.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run.task

    assert run.status == "cancelled"
    assert types_of(run.events)[-1] == "end"


async def test_a_subscriber_sees_the_run_live(stub_agents):  # noqa: F811
    run = runs.registry.create(forecast_input())
    q = run.subscribe()
    run.task = asyncio.create_task(runs.execute(run))
    await run.task

    seen: list[RunEvent] = []
    while not q.empty():
        seen.append(q.get_nowait())

    assert [e.seq for e in seen] == list(range(1, run.seq + 1))
    assert seen[-1].type == "end"


async def test_the_run_header_tracks_the_live_stage(stub_agents):  # noqa: F811
    """`stage` events are the only thing that moves the header, which is why the graph
    emits them rather than a caller inferring them."""
    run = runs.start(forecast_input())
    await run.task

    # Cleared on success — a finished run is not sitting in a stage.
    assert run.stage == ""
    assert run.summary().status == "done"
