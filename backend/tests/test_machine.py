"""The state machine: gates, materialization, retry, cancel, and the stage timeout.

Agents are stubbed at the `stages` seam — these tests verify transitions, not LLMs.
"""

from __future__ import annotations

import asyncio

import pytest

from app import db, machine
from superforecaster import stages
from superforecaster.errors import StageTimeout

from .gated_factories import (
    base_rate_payload,
    chosen_lenses,
    decomposition,
    future,
    inside_payload,
    synthesis_payload,
)


def _run(**kwargs) -> dict:
    defaults = dict(
        question="Will it happen?",
        resolution_criteria="It observably happens.",
        resolution_source="the registry",
        resolution_date=future(),
    )
    return db.create_gated_run(**{**defaults, **kwargs})


def _step(
    run_id: str, stage: str, sub_question_id: str = "", lens_name: str = ""
) -> dict:
    for s in db.list_steps(run_id):
        if (
            s["stage"] == stage
            and s["sub_question_id"] == sub_question_id
            and s["lens_name"] == lens_name
        ):
            return s
    raise AssertionError(f"no {stage} step for ({sub_question_id!r}, {lens_name!r})")


@pytest.fixture
def stub_stages(monkeypatch):
    """Every stage function returns a canned payload instantly."""

    async def fake_decompose(input, deps):
        return decomposition()

    async def fake_lenses(input, decomp, sub_question, deps):
        return chosen_lenses("lens-a", "lens-b")

    async def fake_base_rate(input, sub_question, lens, deps):
        return base_rate_payload(lens.name, sub_question.id)

    async def fake_inside(input, sub_question, payload, deps):
        return inside_payload(payload.lens.name, sub_question.id)

    async def fake_synthesis(input, decomp, base_cells, inside_cells, deps):
        return synthesis_payload()

    monkeypatch.setattr(stages, "run_decompose_stage", fake_decompose)
    monkeypatch.setattr(stages, "run_lenses_stage", fake_lenses)
    monkeypatch.setattr(stages, "run_base_rate_step", fake_base_rate)
    monkeypatch.setattr(stages, "run_inside_step", fake_inside)
    monkeypatch.setattr(stages, "run_synthesis_stage", fake_synthesis)


# ---------- start gate ----------


def test_start_requires_all_four_fields():
    run = db.create_gated_run(question="Only a question")
    with pytest.raises(machine.GateError, match="missing"):
        machine.start_run(run["id"])
    # Still in backlog, untouched.
    assert db.get_gated_run(run["id"])["status"] == "backlog"


def test_start_materializes_the_decompose_step():
    run = _run()
    detail = machine.start_run(run["id"])
    assert detail["status"] == "active"
    assert [s["stage"] for s in detail["steps"]] == ["decompose"]
    assert detail["steps"][0]["status"] == "pending"


# ---------- materialization (advance) ----------


@pytest.mark.asyncio
async def test_decompose_materializes_one_lenses_step_per_researchable(stub_stages):
    run = _run()
    machine.start_run(run["id"])
    await machine.execute_step(_step(run["id"], "decompose")["id"])

    lenses_steps = [s for s in db.list_steps(run["id"]) if s["stage"] == "lenses"]
    # decomposition() has sq1 and sq3 researchable, sq2 judgment.
    assert {s["sub_question_id"] for s in lenses_steps} == {"sq1", "sq3"}


@pytest.mark.asyncio
async def test_last_lenses_step_materializes_base_rate_cells(stub_stages):
    run = _run()
    machine.start_run(run["id"])
    await machine.execute_step(_step(run["id"], "decompose")["id"])
    await machine.execute_step(_step(run["id"], "lenses", "sq1")["id"])
    assert not any(s["stage"] == "base_rates" for s in db.list_steps(run["id"]))

    await machine.execute_step(_step(run["id"], "lenses", "sq3")["id"])
    cells = [s for s in db.list_steps(run["id"]) if s["stage"] == "base_rates"]
    assert {(s["sub_question_id"], s["lens_name"]) for s in cells} == {
        ("sq1", "lens-a"),
        ("sq1", "lens-b"),
        ("sq3", "lens-a"),
        ("sq3", "lens-b"),
    }


@pytest.mark.asyncio
async def test_zero_researchable_bypasses_straight_to_synthesis(
    stub_stages, monkeypatch
):
    async def all_judgment(input, deps):
        return decomposition(knowabilities=("judgment", "judgment", "judgment"))

    monkeypatch.setattr(stages, "run_decompose_stage", all_judgment)
    run = _run()
    machine.start_run(run["id"])
    await machine.execute_step(_step(run["id"], "decompose")["id"])

    stages_present = {s["stage"] for s in db.list_steps(run["id"])}
    assert stages_present == {"decompose", "synthesis"}


@pytest.mark.asyncio
async def test_full_run_lands_complete_with_a_saved_forecast(stub_stages):
    run = _run()
    machine.start_run(run["id"])
    await machine.execute_step(_step(run["id"], "decompose")["id"])
    for sc in ("sq1", "sq3"):
        await machine.execute_step(_step(run["id"], "lenses", sc)["id"])
    for sc in ("sq1", "sq3"):
        for lens in ("lens-a", "lens-b"):
            await machine.execute_step(_step(run["id"], "base_rates", sc, lens)["id"])
    for sc in ("sq1", "sq3"):
        for lens in ("lens-a", "lens-b"):
            await machine.execute_step(_step(run["id"], "inside_view", sc, lens)["id"])
    await machine.execute_step(_step(run["id"], "synthesis")["id"])

    fresh = db.get_gated_run(run["id"])
    assert fresh["status"] == "complete"
    assert fresh["forecast_id"] is not None
    assert db.get_forecast(fresh["forecast_id"]) is not None


# ---------- gating enforcement ----------


@pytest.mark.asyncio
async def test_cannot_run_a_cell_while_prior_stage_is_incomplete(stub_stages):
    run = _run()
    machine.start_run(run["id"])
    await machine.execute_step(_step(run["id"], "decompose")["id"])
    await machine.execute_step(_step(run["id"], "lenses", "sq1")["id"])
    await machine.execute_step(_step(run["id"], "lenses", "sq3")["id"])
    # Force an inside_view row into existence while base_rates is still pending —
    # the defense-in-depth path a stale client could hit.
    db.insert_steps(run["id"], [("inside_view", "sq1", "lens-a")])

    with pytest.raises(machine.GateError, match="gate not satisfied"):
        await machine.execute_step(
            _step(run["id"], "inside_view", "sq1", "lens-a")["id"]
        )


@pytest.mark.asyncio
async def test_cannot_execute_on_a_backlog_run(stub_stages):
    run = _run()
    db.insert_steps(run["id"], [("decompose", "", "")])
    with pytest.raises(machine.GateError, match="backlog"):
        await machine.execute_step(_step(run["id"], "decompose")["id"])


@pytest.mark.asyncio
async def test_complete_step_cannot_be_claimed_twice(stub_stages):
    run = _run()
    machine.start_run(run["id"])
    step_id = _step(run["id"], "decompose")["id"]
    await machine.execute_step(step_id)
    with pytest.raises(machine.GateError, match="claimed twice"):
        await machine.execute_step(step_id)


@pytest.mark.asyncio
async def test_only_one_step_in_flight_globally(stub_stages, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_decompose(input, deps):
        started.set()
        await release.wait()
        return decomposition()

    monkeypatch.setattr(stages, "run_decompose_stage", slow_decompose)
    run_a, run_b = _run(), _run()
    machine.start_run(run_a["id"])
    machine.start_run(run_b["id"])

    task = asyncio.create_task(
        machine.execute_step(_step(run_a["id"], "decompose")["id"])
    )
    await started.wait()
    with pytest.raises(machine.BusyError):
        await machine.execute_step(_step(run_b["id"], "decompose")["id"])
    release.set()
    await task


# ---------- failure, retry, cancel, timeout ----------


@pytest.mark.asyncio
async def test_failure_lands_on_the_step_and_the_red_chip(stub_stages, monkeypatch):
    async def explode(input, deps):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(stages, "run_decompose_stage", explode)
    run = _run()
    machine.start_run(run["id"])
    step_id = _step(run["id"], "decompose")["id"]

    with pytest.raises(RuntimeError):
        await machine.execute_step(step_id)

    assert db.get_step(step_id)["status"] == "error"
    assert "provider exploded" in db.get_gated_run(run["id"])["error"]


@pytest.mark.asyncio
async def test_errored_step_is_retryable_and_recovers(stub_stages, monkeypatch):
    calls = {"n": 0}

    async def flaky(input, deps):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first attempt dies")
        return decomposition()

    monkeypatch.setattr(stages, "run_decompose_stage", flaky)
    run = _run()
    machine.start_run(run["id"])
    step_id = _step(run["id"], "decompose")["id"]

    with pytest.raises(RuntimeError):
        await machine.execute_step(step_id)
    finished = await machine.execute_step(step_id)

    assert finished["status"] == "complete"
    assert finished["attempts"] == 2
    assert db.get_gated_run(run["id"])["error"] is None


@pytest.mark.asyncio
async def test_cancel_lands_cancelled_and_step_is_reclaimable(stub_stages, monkeypatch):
    started = asyncio.Event()

    async def hang(input, deps):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(stages, "run_decompose_stage", hang)
    run = _run()
    machine.start_run(run["id"])
    step_id = _step(run["id"], "decompose")["id"]

    task = asyncio.create_task(machine.execute_step(step_id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    step = db.get_step(step_id)
    assert step["status"] == "error"
    assert step["error"] == "cancelled"
    assert db.claim_step(step_id) is not None


@pytest.mark.asyncio
async def test_stage_timeout_marks_the_step(stub_stages, monkeypatch):
    monkeypatch.setenv("STAGE_TIMEOUT_SECONDS", "0.05")

    async def too_slow(input, deps):
        await asyncio.sleep(1.0)
        return decomposition()

    monkeypatch.setattr(stages, "run_decompose_stage", too_slow)
    run = _run()
    machine.start_run(run["id"])
    step_id = _step(run["id"], "decompose")["id"]

    with pytest.raises(StageTimeout):
        await machine.execute_step(step_id)

    step = db.get_step(step_id)
    assert step["status"] == "error"
    assert "StageTimeout" in step["error"]
