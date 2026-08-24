"""The update cycle's routing: resolution blocks the update, a large move is verified
exactly once, and the write gate refuses noise and contradictions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from superforecaster import update as uc
from superforecaster.models import (
    EvidenceItem,
    ForecastRecord,
    ForecastUpdateRecord,
    ResolutionCheckResult,
    UpdateDecision,
)
from tests.test_checks import sub


def a_record(probability: float = 0.50) -> ForecastRecord:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ForecastRecord(
        id="fc_1",
        question="Will A acquire B?",
        resolution_criteria="Deal closes",
        resolution_source="SEC",
        category="business",
        submission_gap_days=7,
        submission_deadline=now,
        resolution_date=datetime(2027, 1, 1, tzinfo=timezone.utc),
        initial_reasoning="r",
        decompositions=[sub(), sub(), sub()],
        updates=[
            ForecastUpdateRecord(
                id="u1",
                forecast_id="fc_1",
                probability=probability,
                reasoning="initial",
                is_late=False,
                created_at=now,
            )
        ],
        created_at=now,
    )


def a_decision(prior: float, posterior: float) -> UpdateDecision:
    """An internally consistent decision: evidence points the way the number moved."""
    if prior == posterior:
        evidence = []
    elif posterior > prior:
        evidence = [EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.2)]
    else:
        evidence = [EvidenceItem(fact="f", source="s", p_if_true=0.2, p_if_false=0.9)]
    return UpdateDecision(
        evidence=evidence, prior=prior, posterior=posterior, reasoning="r"
    )


@pytest.fixture
def stub(monkeypatch):
    """Stub the two agents. Returns knobs and call counts."""
    state = {
        "appears_resolved": False,
        "decision": a_decision(0.50, 0.60),
        "verify_decision": None,
        "update_calls": 0,
        "verify_calls": 0,
    }

    async def fake_resolution(record, deps):
        return ResolutionCheckResult(
            appears_resolved=state["appears_resolved"],
            confidence="low",
            reasoning="stub",
        )

    async def fake_update(record, deps, *, verify=None):
        if verify is not None:
            state["verify_calls"] += 1
            return state["verify_decision"] or state["decision"]
        state["update_calls"] += 1
        return state["decision"]

    monkeypatch.setattr(uc, "run_resolution_check", fake_resolution)
    monkeypatch.setattr(uc, "run_update", fake_update)
    return state


async def test_resolved_forecast_never_reaches_the_update_step(stub):
    stub["appears_resolved"] = True
    out = await uc.run_update_cycle(a_record())

    assert stub["update_calls"] == 0
    assert out.flagged_resolved is True
    assert out.updated is False


async def test_unresolved_forecast_proceeds_to_the_update(stub):
    out = await uc.run_update_cycle(a_record())
    assert stub["update_calls"] == 1
    assert (out.updated, out.new_probability, out.reasoning) == (True, 0.60, "r")


async def test_large_move_is_verified_exactly_once(stub):
    """FTX filing is a legitimate 0.20 -> 0.99 move; corroborate, do not cap."""
    stub["decision"] = a_decision(0.10, 0.95)
    stub["verify_decision"] = a_decision(0.10, 0.93)
    out = await uc.run_update_cycle(a_record(0.10))

    assert stub["verify_calls"] == 1
    assert (out.updated, out.new_probability) == (True, 0.93)


async def test_a_walked_back_large_move_reports_the_revised_value(stub):
    stub["decision"] = a_decision(0.10, 0.95)
    stub["verify_decision"] = a_decision(0.10, 0.30)
    out = await uc.run_update_cycle(a_record(0.10))

    assert (out.updated, out.new_probability) == (True, 0.30)


async def test_normal_move_skips_verification(stub):
    await uc.run_update_cycle(a_record())
    assert stub["verify_calls"] == 0


async def test_large_move_threshold_is_configurable(stub, monkeypatch):
    monkeypatch.setenv("CHECK_LARGE_MOVE", "0.05")
    await uc.run_update_cycle(a_record())
    assert stub["verify_calls"] == 1


async def test_sub_threshold_move_is_treated_as_noise(stub):
    """Principle 10 — the 3-point gate that filters rephrasing of the same view."""
    stub["decision"] = a_decision(0.50, 0.51)
    out = await uc.run_update_cycle(a_record())

    assert out.updated is False
    assert "noise" in out.reason


async def test_no_evidence_and_no_movement_is_a_clean_no_op(stub):
    stub["decision"] = a_decision(0.50, 0.50)
    out = await uc.run_update_cycle(a_record())

    assert out.updated is False
    assert out.violations == []


async def test_an_internally_inconsistent_update_is_refused(stub):
    """Principle 11 — evidence points up, the number went down. Refuse the write."""
    stub["decision"] = UpdateDecision(
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.1)],
        prior=0.50,
        posterior=0.30,
        reasoning="contradicts itself",
    )
    out = await uc.run_update_cycle(a_record())

    assert out.updated is False
    assert 11 in {v.principle for v in out.violations}
