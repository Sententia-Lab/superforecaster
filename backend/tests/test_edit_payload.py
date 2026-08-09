"""Editable review: reconcile, the edit lock, weight normalization, and the endpoint.

`reconcile` replaced `advance`, so the forward-path guarantees live in `test_machine.py`
and stay untouched. What is covered here is everything an edit added: rows that have to
disappear, rows that must never disappear, and the arithmetic a hand-written lens set has
to satisfy.
"""

from __future__ import annotations

import httpx
import pytest

from api.main import app
from superforecaster import db, machine
from superforecaster.models import Decomposition, Lens, SubQuestionLensesEdit
from superforecaster.stages import normalize_weights

from .gated_factories import (
    base_rate_payload,
    chosen_lenses,
    decomposition,
    future,
    lens,
    sub,
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


def _keys(run_id: str, stage: str) -> set[tuple[str, str]]:
    return {
        (s["sub_question_id"], s["lens_name"])
        for s in db.list_steps(run_id)
        if s["stage"] == stage
    }


def _decomposed(knowabilities=("researchable", "researchable", "judgment")) -> str:
    """A started run whose decompose step is complete. Returns the run id."""
    run = _run()
    machine.start_run(run["id"])
    db.finish_step(
        _step(run["id"], "decompose")["id"],
        decomposition(knowabilities).model_dump_json(),
    )
    machine.reconcile(run["id"])
    return run["id"]


def _lensed(*, sq1=("lens-a", "lens-b"), sq2=("lens-c",)) -> str:
    """A run with every lens step complete, so the base-rate cells exist."""
    run_id = _decomposed()
    db.finish_step(
        _step(run_id, "lenses", "sq1")["id"], chosen_lenses(*sq1).model_dump_json()
    )
    db.finish_step(
        _step(run_id, "lenses", "sq2")["id"], chosen_lenses(*sq2).model_dump_json()
    )
    machine.reconcile(run_id)
    return run_id


# ---------- reconcile ----------


def test_reconcile_adds_the_next_stage_and_is_idempotent():
    run_id = _decomposed()
    assert _keys(run_id, "lenses") == {("sq1", ""), ("sq2", "")}

    before = [s["id"] for s in db.list_steps(run_id)]
    machine.reconcile(run_id)
    assert [s["id"] for s in db.list_steps(run_id)] == before


def test_reconcile_deletes_a_row_the_payload_no_longer_implies():
    run_id = _decomposed()
    assert _keys(run_id, "lenses") == {("sq1", ""), ("sq2", "")}

    # sq2 becomes judgment, so it should no longer carry a lens row.
    edited = Decomposition(
        sub_questions=[sub("sq1"), sub("sq2", "judgment"), sub("sq3")],
        chain_rule="conjunction",
        chain_note="all must hold",
    )
    db.edit_step_payload(_step(run_id, "decompose")["id"], edited.model_dump_json())
    machine.reconcile(run_id)

    assert _keys(run_id, "lenses") == {("sq1", ""), ("sq3", "")}


def test_reconcile_refuses_to_delete_a_row_that_holds_work():
    run_id = _lensed()
    # Bypass the lock the way only a bug could: research a cell, then strand it.
    db.finish_step(
        _step(run_id, "base_rates", "sq1", "lens-b")["id"],
        base_rate_payload("lens-b", "sq1").model_dump_json(),
    )
    db.edit_step_payload(
        _step(run_id, "lenses", "sq1")["id"],
        chosen_lenses("lens-a").model_dump_json(),
    )

    with pytest.raises(machine.GateError, match="cannot discard"):
        machine.reconcile(run_id)

    # Nothing deleted and nothing inserted — the raise came before both writes.
    assert ("sq1", "lens-b") in _keys(run_id, "base_rates")


# ---------- editing the decomposition ----------


def test_removing_a_sub_question_drops_its_pending_lens_row():
    run_id = _decomposed()
    edited = Decomposition(
        sub_questions=[sub("sq1"), sub("sq2"), sub("sq3")],
        chain_rule="conjunction",
        chain_note="all must hold",
    )
    machine.edit_payload(run_id, _step(run_id, "decompose")["id"], edited.model_dump())
    assert _keys(run_id, "lenses") == {("sq1", ""), ("sq2", ""), ("sq3", "")}

    shorter = Decomposition(
        sub_questions=[sub("sq1"), sub("sq2"), sub("sq9")],
        chain_rule="conjunction",
        chain_note="all must hold",
    )
    machine.edit_payload(run_id, _step(run_id, "decompose")["id"], shorter.model_dump())
    # Ids are re-stamped by position, so the third slot is sq3 whatever it was called.
    assert _keys(run_id, "lenses") == {("sq1", ""), ("sq2", ""), ("sq3", "")}


def test_editing_the_decomposition_is_blocked_once_a_lens_step_has_run():
    run_id = _decomposed()
    db.finish_step(
        _step(run_id, "lenses", "sq1")["id"], chosen_lenses("lens-a").model_dump_json()
    )

    edited = Decomposition(
        sub_questions=[sub("sq1"), sub("sq2"), sub("sq3")],
        chain_rule="conjunction",
        chain_note="all must hold",
    )
    with pytest.raises(machine.GateError, match="lenses step .* is complete"):
        machine.edit_payload(
            run_id, _step(run_id, "decompose")["id"], edited.model_dump()
        )


# ---------- editing a lens set ----------


def test_editing_a_lens_set_rekeys_only_its_own_cells():
    run_id = _lensed()
    assert _keys(run_id, "base_rates") == {
        ("sq1", "lens-a"),
        ("sq1", "lens-b"),
        ("sq2", "lens-c"),
    }

    machine.edit_payload(
        run_id,
        _step(run_id, "lenses", "sq1")["id"],
        SubQuestionLensesEdit(
            lenses=[
                lens("lens-a").model_copy(update={"weight": 0.6}),
                lens("recent").model_copy(update={"weight": 0.4}),
            ]
        ).model_dump(),
    )

    assert _keys(run_id, "base_rates") == {
        ("sq1", "lens-a"),
        ("sq1", "recent"),
        ("sq2", "lens-c"),
    }


def test_every_lens_set_is_editable_while_no_rate_has_come_back():
    run_id = _lensed()
    one_lens = SubQuestionLensesEdit(lenses=[lens("solo")]).model_dump()

    machine.edit_payload(run_id, _step(run_id, "lenses", "sq1")["id"], one_lens)
    machine.edit_payload(run_id, _step(run_id, "lenses", "sq2")["id"], one_lens)

    assert _keys(run_id, "base_rates") == {("sq1", "solo"), ("sq2", "solo")}


def test_one_measured_base_rate_locks_every_lens_set():
    """Not just its own sub-question's.

    Populations are chosen before they are measured (ADR 40). Once any rate is back the
    run has seen an answer, so re-choosing populations anywhere — including a sub-question
    nobody has measured yet — is choosing them with that answer in hand.
    """
    run_id = _lensed()
    db.finish_step(
        _step(run_id, "base_rates", "sq1", "lens-a")["id"],
        base_rate_payload("lens-a", "sq1").model_dump_json(),
    )

    one_lens = SubQuestionLensesEdit(lenses=[lens("solo")]).model_dump()
    for sub_question_id in ("sq1", "sq2"):
        with pytest.raises(machine.GateError, match="base_rates step .* is complete"):
            machine.edit_payload(
                run_id, _step(run_id, "lenses", sub_question_id)["id"], one_lens
            )


def test_an_edited_weight_reaches_the_step_that_consumes_it():
    run_id = _lensed(sq1=("lens-a", "lens-b"))
    machine.edit_payload(
        run_id,
        _step(run_id, "lenses", "sq1")["id"],
        SubQuestionLensesEdit(
            lenses=[
                lens("lens-a").model_copy(update={"weight": 0.75}),
                lens("lens-b").model_copy(update={"weight": 0.25}),
            ]
        ).model_dump(),
    )

    chosen = machine._chosen_lens(db.list_steps(run_id), "sq1", "lens-a")
    assert chosen.weight == 0.75


def test_edit_records_the_human_without_touching_status_or_attempts():
    run_id = _decomposed()
    before = _step(run_id, "decompose")
    assert before["edited_at"] is None

    edited = Decomposition(
        sub_questions=[sub("sq1"), sub("sq2"), sub("sq3")],
        chain_rule="conjunction",
        chain_note="all must hold",
    )
    machine.edit_payload(run_id, before["id"], edited.model_dump())

    after = _step(run_id, "decompose")
    assert after["edited_at"] is not None
    assert after["status"] == "complete"
    assert after["attempts"] == before["attempts"]


def test_a_pending_step_has_no_payload_to_edit():
    run_id = _decomposed()
    with pytest.raises(machine.GateError, match="there is no payload to edit"):
        machine.edit_payload(
            run_id,
            _step(run_id, "lenses", "sq1")["id"],
            SubQuestionLensesEdit(lenses=[lens("solo")]).model_dump(),
        )


def test_only_decompose_and_lenses_are_editable():
    run_id = _lensed()
    step = _step(run_id, "base_rates", "sq1", "lens-a")
    with pytest.raises(machine.GateError, match="not editable"):
        machine.edit_payload(run_id, step["id"], {})


# ---------- what a hand-written payload has to satisfy ----------


def test_hand_written_lens_weights_must_sum_to_one():
    with pytest.raises(ValueError, match="must sum to 1.00, got 1.05"):
        SubQuestionLensesEdit(
            lenses=[
                lens("a").model_copy(update={"weight": 0.55}),
                lens("b").model_copy(update={"weight": 0.50}),
            ]
        )


def test_lens_names_must_be_unique_within_a_sub_question():
    with pytest.raises(ValueError, match="names must be unique"):
        SubQuestionLensesEdit(
            lenses=[
                lens("same").model_copy(update={"weight": 0.5}),
                lens("same").model_copy(update={"weight": 0.5}),
            ]
        )


@pytest.mark.parametrize("count", [2, 6])
def test_a_decomposition_stays_between_three_and_five(count):
    with pytest.raises(ValueError):
        Decomposition(
            sub_questions=[sub(f"sq{i}") for i in range(count)],
            chain_rule="conjunction",
            chain_note="all must hold",
        )


# ---------- weight normalization ----------


def _weights(*values: float) -> list[Lens]:
    return [
        lens(f"l{i}").model_copy(update={"weight": w}) for i, w in enumerate(values)
    ]


def test_agent_weights_are_rescaled_to_sum_to_one():
    out = normalize_weights(_weights(0.9, 0.6, 0.4))
    assert [l.weight for l in out] == [0.47, 0.32, 0.21]
    assert sum(l.weight for l in out) == 1.0


def test_every_share_lands_within_a_penny_of_its_exact_value():
    """Largest remainder's guarantee: the set sums to 1 and no lens drifts by 2 pennies."""
    before = _weights(0.9, 0.6, 0.4)
    total = sum(l.weight for l in before)
    for original, rescaled in zip(before, normalize_weights(before)):
        assert abs(rescaled.weight - original.weight / total) <= 0.01


def test_three_equal_lenses_still_sum_to_exactly_one():
    out = normalize_weights(_weights(0.5, 0.5, 0.5))
    assert sorted(l.weight for l in out) == [0.33, 0.33, 0.34]
    assert sum(l.weight for l in out) == 1.0


def test_a_lopsided_set_floors_at_a_penny_never_zero():
    out = normalize_weights(_weights(0.99, 0.001, 0.001))
    assert min(l.weight for l in out) >= 0.01
    assert sum(l.weight for l in out) == 1.0


def test_a_single_lens_carries_the_whole_weight():
    assert [l.weight for l in normalize_weights(_weights(0.4))] == [1.0]


# ---------- the endpoint ----------


pytestmark_asyncio = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _payload_url(run_id: str, step_id: str) -> str:
    return f"/runs/{run_id}/steps/{step_id}/payload"


@pytest.mark.asyncio
async def test_put_payload_returns_the_whole_run(client):
    run_id = _decomposed()
    edited = Decomposition(
        sub_questions=[sub("sq1"), sub("sq2"), sub("sq3")],
        chain_rule="conjunction",
        chain_note="all must hold",
    )
    resp = await client.put(
        _payload_url(run_id, _step(run_id, "decompose")["id"]),
        json=edited.model_dump(mode="json"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert {s["sub_question_id"] for s in body["steps"] if s["stage"] == "lenses"} == {
        "sq1",
        "sq2",
        "sq3",
    }
    decompose_step = next(s for s in body["steps"] if s["stage"] == "decompose")
    assert decompose_step["edited_at"] is not None


@pytest.mark.asyncio
async def test_put_payload_404s_for_a_step_on_another_run(client):
    run_id = _decomposed()
    other = _decomposed()
    resp = await client.put(
        _payload_url(other, _step(run_id, "decompose")["id"]), json={}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_payload_409s_once_something_downstream_ran(client):
    run_id = _decomposed()
    db.finish_step(
        _step(run_id, "lenses", "sq1")["id"], chosen_lenses("lens-a").model_dump_json()
    )
    edited = Decomposition(
        sub_questions=[sub("sq1"), sub("sq2"), sub("sq3")],
        chain_rule="conjunction",
        chain_note="all must hold",
    )
    resp = await client.put(
        _payload_url(run_id, _step(run_id, "decompose")["id"]),
        json=edited.model_dump(mode="json"),
    )
    assert resp.status_code == 409
    assert "is complete" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_put_payload_422s_on_weights_that_do_not_sum_to_one(client):
    run_id = _lensed()
    resp = await client.put(
        _payload_url(run_id, _step(run_id, "lenses", "sq1")["id"]),
        json={
            "lenses": [
                lens("a").model_copy(update={"weight": 0.55}).model_dump(mode="json"),
                lens("b").model_copy(update={"weight": 0.50}).model_dump(mode="json"),
            ]
        },
    )
    assert resp.status_code == 422
    assert "sum to 1.00" in str(resp.json()["detail"])


def test_with_ids_stamps_sq1_upwards():
    """ADR 56. The ids are assigned by position, never asked of the model — a model asked
    for keys eventually hands back two of one and skips another."""
    from superforecaster.agents.decompose import with_ids

    d = Decomposition(
        sub_questions=[sub("whatever"), sub("also-wrong"), sub("")],
        chain_rule="conjunction",
        chain_note="all must hold",
    )

    assert [s.id for s in with_ids(d).sub_questions] == ["sq1", "sq2", "sq3"]
