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
    assert run._subscribers == set()  # the slow subscriber was dropped


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


# ---------- projections ----------


def test_decomposition_projects_sub_claims_then_the_chain_note():
    run = a_run()
    runs.project_decompose(run, a_decomposition())

    assert types_of(run.events) == ["sub", "sub", "sub", "note"]
    assert run.events[-1].payload["label"] == "chain_note"


def test_outside_view_groups_its_classes_under_the_sub_claims():
    """One `claim` per sub-claim, not a flat list of reference classes.

    A reader's question is "which part of this did you look up, and what did you find" —
    a flat list cannot answer it.
    """
    run = a_run()
    d = a_decomposition()
    runs.project_outside(run, d, an_outside_view())

    claims = [e for e in run.events if e.type == "claim"]
    assert len(claims) == len(d.sub_claims) + 1  # +1 for the unattributed classes
    assert run.events[-1].payload["label"] == "aggregate_base_rate — 22%"


def test_a_sub_claim_nobody_researched_carries_no_rate():
    """None, not a fabricated number — for a judgment sub-claim that is the right answer."""
    run = a_run()
    runs.project_outside(run, a_decomposition(), an_outside_view())

    by_id = {e.payload["id"]: e.payload for e in run.events if e.type == "claim"}
    assert by_id["sc1"]["rate"] is None
    assert by_id["sc1"]["classes"] == []


def test_a_researched_sub_claim_carries_the_weighted_rate_of_its_classes():
    run = a_run()
    d = a_decomposition()
    o = an_outside_view()
    o.reference_classes[0].sub_claim_ids = ["sc1"]
    o.reference_classes[1].sub_claim_ids = ["sc1"]
    runs.project_outside(run, d, o)

    sc1 = next(e.payload for e in run.events if e.payload.get("id") == "sc1")
    assert sc1["rate"] == pytest.approx(0.22)
    assert len(sc1["classes"]) == 2


def test_the_anchor_note_carries_the_disagreement_when_there_is_one():
    """P7's required sentence is what the UI shows, not a restatement of it."""
    run = a_run()
    outside = an_outside_view().model_copy(
        update={"disagreement": "the narrow class binds"}
    )
    runs.project_outside(run, a_decomposition(), outside)

    assert run.events[-1].payload["text"] == "the narrow class binds"


def test_inside_view_projects_adjustments_notes_and_all_five_biases():
    run = a_run()
    runs.project_inside(run, an_inside_view())

    assert types_of(run.events) == ["adj", "adj", "note", "note"] + ["bias"] * 5


def test_a_noise_adjustment_is_projected_as_moving_nothing():
    run = a_run()
    inside = an_inside_view()
    inside.adjustments[0].is_noise = True
    runs.project_inside(run, inside)

    assert run.events[0].payload["mag"] == 0.0
    assert run.events[0].payload["noise"] is True


def test_critique_projects_every_check_including_passes():
    run = a_run()
    state = ForecastState(input=forecast_input())
    state.decomposition = a_decomposition()
    state.outside = an_outside_view()
    state.inside = an_inside_view()
    state.forecast = a_forecast(0.28)
    state.synthesis_attempts = 1

    runs.project_critique(run, state)

    checks_emitted = [e for e in run.events if e.type == "check"]
    assert len(checks_emitted) == len(checks.FORECAST_CHECK_LABELS)
    assert all(e.payload["ok"] for e in checks_emitted)
    assert not [e for e in run.events if e.type == "route"]


def test_a_blocking_violation_on_attempt_one_emits_a_route_event():
    run = a_run()
    state = ForecastState(input=forecast_input())
    state.decomposition = a_decomposition()
    state.outside = an_outside_view()
    state.inside = an_inside_view()
    state.forecast = a_forecast(0.95)  # nowhere near the implied 0.28
    state.synthesis_attempts = 1

    runs.project_critique(run, state)

    route = [e for e in run.events if e.type == "route"]
    assert len(route) == 1
    assert "attempt 2 of 2" in route[0].payload["text"]


def test_no_route_event_once_the_retry_budget_is_spent():
    run = a_run()
    state = ForecastState(input=forecast_input())
    state.decomposition = a_decomposition()
    state.outside = an_outside_view()
    state.inside = an_inside_view()
    state.forecast = a_forecast(0.95)
    state.synthesis_attempts = fg.MAX_SYNTHESIS_ATTEMPTS

    runs.project_critique(run, state)
    assert not [e for e in run.events if e.type == "route"]


# ---------- the waterfall ----------


def test_waterfall_walks_the_anchor_to_the_stated_probability():
    outside, inside = an_outside_view(), an_inside_view()
    forecast = a_forecast(0.28)
    rows = runs.build_waterfall(outside, inside, forecast)

    assert [r["kind"] for r in rows] == ["anchor", "up", "down", "final"]
    assert rows[0]["running"] == pytest.approx(0.22)
    assert rows[-1]["running"] == pytest.approx(0.28)


def test_waterfall_and_check_derivation_agree_on_the_implied_value():
    """The chart and the check must never tell different stories about the evidence."""
    outside, inside = an_outside_view(), an_inside_view()
    rows = runs.build_waterfall(outside, inside, a_forecast(0.28))

    last_adjustment_total = rows[-2]["running"]
    assert last_adjustment_total == pytest.approx(
        checks.implied_probability(outside, inside)
    )


def test_waterfall_skips_noise_adjustments():
    outside, inside = an_outside_view(), an_inside_view()
    inside.adjustments[0].is_noise = True
    rows = runs.build_waterfall(outside, inside, a_forecast(0.18))

    assert [r["kind"] for r in rows] == ["anchor", "down", "final"]


# ---------- driving a run ----------


async def test_a_full_run_emits_every_stage_and_ends_done(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    await run.task

    assert run.status == "done"
    assert run.forecast_id is not None

    stages = [e.payload["stage"] for e in run.events if e.type == "stage"]
    assert stages == ["decompose", "outside", "inside", "synth", "critique"]
    assert types_of(run.events)[-2:] == ["result", "end"]


async def test_the_result_event_carries_a_waterfall_and_an_anchor(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    await run.task

    result = [e for e in run.events if e.type == "result"][0].payload
    assert result["anchor"] == pytest.approx(0.22)
    assert result["probability"] == pytest.approx(0.28)
    assert result["waterfall"][-1]["kind"] == "final"


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

    monkeypatch.setattr(fg, "run_outside_view", boom)

    run = runs.start(forecast_input())
    await run.task

    assert run.status == "error"
    assert "provider exploded" in run.error
    assert types_of(run.events)[-2:] == ["error", "end"]


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


# ---------- what attempt 2 is told ----------


def test_retry_brief_is_the_real_prompt_text_not_a_description():
    """Built from the same formatters run_synthesize uses, so the two cannot drift."""
    from superforecaster.agents import synthesize
    from superforecaster.models import CheckViolation

    outside, inside = an_outside_view(), an_inside_view()
    violation = CheckViolation(
        principle=6, name="derivation", detail="stated 0.120 vs implied 0.280"
    )
    brief = synthesize.retry_brief(outside, inside, [violation])

    assert brief["anchor"] == 0.22
    assert brief["implied"] == pytest.approx(0.28)
    assert "Principle 6 (derivation)" in brief["correction"]
    assert "stated 0.120 vs implied 0.280" in brief["correction"]
    # The prompt's own promise, verbatim — this is what makes it a correction and not
    # a re-roll, so it has to be what the user sees.
    assert "Do not start over" in brief["correction"]
    assert brief["correction"] in synthesize._violation_block([violation])


async def test_the_second_synthesis_emits_a_brief(monkeypatch, stub_agents):  # noqa: F811
    async def wandering(input, d, o, i, violations, deps):
        stub_agents["synthesize"] += 1
        return a_forecast(0.95 if stub_agents["synthesize"] == 1 else 0.28)

    monkeypatch.setattr(fg, "run_synthesize", wandering)
    run = runs.start(forecast_input())
    await run.task

    briefs = [e for e in run.events if e.type == "brief"]
    assert len(briefs) == 1
    assert briefs[0].attempt == 2
    assert briefs[0].stage == "synth"
    assert "Principle 6" in briefs[0].payload["correction"]


async def test_the_brief_arrives_before_the_corrected_draft(monkeypatch, stub_agents):  # noqa: F811
    """It explains the attempt it precedes, so ordering is the whole point."""

    async def wandering(input, d, o, i, violations, deps):
        stub_agents["synthesize"] += 1
        return a_forecast(0.95 if stub_agents["synthesize"] == 1 else 0.28)

    monkeypatch.setattr(fg, "run_synthesize", wandering)
    run = runs.start(forecast_input())
    await run.task

    second_attempt = [e for e in run.events if e.attempt == 2 and e.stage == "synth"]
    assert types_of(second_attempt) == ["stage", "brief", "draft"]


async def test_a_clean_run_emits_no_brief(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    await run.task

    assert not [e for e in run.events if e.type == "brief"]


async def test_check_events_carry_their_evidence(stub_agents):  # noqa: F811
    run = runs.start(forecast_input())
    await run.task

    derivation = [
        e
        for e in run.events
        if e.type == "check" and e.payload["name"] == "derivation"
    ][0]
    assert derivation.payload["evidence"]["anchor"] == 0.22
    assert derivation.payload["evidence"]["walk"]
