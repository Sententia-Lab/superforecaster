"""Tests for the update graph's wiring.

Two routing rules carry real consequences and are asserted here:

1. **Resolution blocks the probability update.** Previously a `flagged_ids` set passed
   between two for-loops; now an edge. A resolved forecast cannot have its probability
   moved because the node that would move it is unreachable.
2. **A large move routes through verification exactly once.** Not capped — decisive
   events are real — but corroborated, and never in an infinite loop.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from superforecaster.graphs import update as ug
from superforecaster.graphs.state import ForecastDeps, UpdateState
from superforecaster.models import (
    EvidenceItem,
    ForecastRecord,
    ForecastUpdateRecord,
    ResearchSummary,
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
        research=ResearchSummary(),
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


def a_decision(
    prior: float, posterior: float, *, confirming: bool = True
) -> UpdateDecision:
    """An internally consistent decision — evidence points the way the number moved."""
    if prior == posterior:
        evidence = []
    elif (posterior > prior) == confirming:
        evidence = [EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.2)]
    else:
        evidence = [EvidenceItem(fact="f", source="s", p_if_true=0.2, p_if_false=0.9)]
    if posterior < prior and confirming:
        evidence = [EvidenceItem(fact="f", source="s", p_if_true=0.2, p_if_false=0.9)]
    return UpdateDecision(
        evidence=evidence, prior=prior, posterior=posterior, reasoning="r"
    )


@pytest.fixture
def stub(monkeypatch):
    """Stub the two agents and the DB writes. Returns knobs and call counts."""
    state = {
        "appears_resolved": False,
        "decision": a_decision(0.50, 0.60),
        "verify_decision": None,
        "update_calls": 0,
        "verify_calls": 0,
        "written": [],
        "marked": [],
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

    monkeypatch.setattr(ug, "run_resolution_check", fake_resolution)
    monkeypatch.setattr(ug, "run_update", fake_update)
    monkeypatch.setattr(
        ug.db,
        "mark_refreshed",
        lambda fid, flagged: state["marked"].append((fid, flagged)),
    )
    monkeypatch.setattr(
        ug.db,
        "add_forecast_update",
        lambda **kw: state["written"].append(kw),
    )
    return state


async def visited(record: ForecastRecord) -> tuple[list[str], UpdateState]:
    seen: list[str] = []
    st = UpdateState(record=record)
    async with ug.update_graph.iter(
        ug.CheckResolved(), state=st, deps=ForecastDeps()
    ) as run:
        async for node in run:
            seen.append(type(node).__name__)
    return seen, st


# ---------- resolution short-circuits ----------


async def test_resolved_forecast_never_reaches_the_update_step(stub):
    """The rule that used to live in a flagged_ids set, now an unreachable node."""
    stub["appears_resolved"] = True
    seen, _ = await visited(a_record())

    assert "ApplyBayes" not in seen
    assert "GuardUpdate" not in seen
    assert stub["update_calls"] == 0


async def test_resolved_forecast_is_flagged_not_closed(stub):
    """Never auto-resolves — an admin confirms."""
    stub["appears_resolved"] = True
    _, st = await visited(a_record())
    assert stub["marked"] == [("fc_1", True)]
    assert stub["written"] == []


async def test_unresolved_forecast_proceeds_to_the_update(stub):
    seen, _ = await visited(a_record())
    assert seen[:3] == ["CheckResolved", "ApplyBayes", "GuardUpdate"]
    assert stub["update_calls"] == 1


# ---------- large-move verification ----------


async def test_large_move_routes_through_verification(stub):
    """FTX filing is a legitimate 0.20 -> 0.99 move; corroborate, don't cap."""
    stub["decision"] = a_decision(0.10, 0.95)
    seen, _ = await visited(a_record(0.10))

    assert "VerifyLargeMove" in seen
    assert stub["verify_calls"] == 1


async def test_verification_happens_at_most_once(stub):
    """Even if the verified decision is still a large move, no second pass."""
    stub["decision"] = a_decision(0.10, 0.95)
    stub["verify_decision"] = a_decision(0.10, 0.93)
    seen, _ = await visited(a_record(0.10))

    assert seen.count("VerifyLargeMove") == 1
    assert seen.count("GuardUpdate") == 2


async def test_a_survived_large_move_is_written(stub):
    stub["decision"] = a_decision(0.10, 0.95)
    _, st = await visited(a_record(0.10))

    assert st.decision.verified_large_move is True
    assert stub["written"][0]["probability"] == 0.95


async def test_a_walked_back_large_move_is_written_at_the_revised_value(stub):
    stub["decision"] = a_decision(0.10, 0.95)
    stub["verify_decision"] = a_decision(0.10, 0.30)
    _, st = await visited(a_record(0.10))

    assert stub["written"][0]["probability"] == 0.30


async def test_normal_move_skips_verification(stub):
    seen, _ = await visited(a_record())
    assert "VerifyLargeMove" not in seen
    assert stub["verify_calls"] == 0


async def test_large_move_threshold_is_configurable(stub, monkeypatch):
    monkeypatch.setenv("CHECK_LARGE_MOVE", "0.05")
    seen, _ = await visited(a_record())
    assert "VerifyLargeMove" in seen


# ---------- the write gate ----------


async def test_material_consistent_update_is_written(stub):
    _, st = await visited(a_record())
    assert stub["written"] == [
        {
            "forecast_id": "fc_1",
            "probability": 0.60,
            "reasoning": "r",
        }
    ]


async def test_sub_threshold_move_is_treated_as_noise(stub):
    """Principle 10 — the 3-point gate that filters rephrasing of the same view."""
    stub["decision"] = a_decision(0.50, 0.51)
    _, st = await visited(a_record())

    assert stub["written"] == []


async def test_no_evidence_and_no_movement_is_a_clean_no_op(stub):
    stub["decision"] = a_decision(0.50, 0.50)
    _, st = await visited(a_record())

    assert stub["written"] == []
    assert st.violations == []


async def test_internally_inconsistent_update_is_not_written(stub):
    """Principle 11 — evidence points up, the number went down. Refuse the write."""
    stub["decision"] = UpdateDecision(
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.1)],
        prior=0.50,
        posterior=0.30,
        reasoning="contradicts itself",
    )
    _, st = await visited(a_record())

    assert stub["written"] == []
    assert 11 in {v.principle for v in st.violations}


async def test_refreshed_timestamp_is_always_marked(stub):
    """Even a no-op run records that the forecast was looked at."""
    stub["decision"] = a_decision(0.50, 0.50)
    await visited(a_record())
    assert stub["marked"] == [("fc_1", False)]


# ---------- diagram ----------


def test_mermaid_shows_both_branches():
    code = ug.update_mermaid()
    assert "CheckResolved --> ApplyBayes" in code
    assert "GuardUpdate --> VerifyLargeMove" in code
    assert "VerifyLargeMove --> GuardUpdate" in code
