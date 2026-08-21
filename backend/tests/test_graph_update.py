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
from pydantic_graph import EndMarker

from superforecaster import update as ug
from superforecaster.deps import ForecastDeps
from superforecaster.update import UpdateState
from superforecaster.models import (
    EvidenceItem,
    ForecastRecord,
    ForecastUpdateRecord,
    ResolutionCheckResult,
    UpdateDecision,
    UpdateOutcome,
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
    """Stub the two agents. Returns knobs and call counts.

    Nothing to stub for storage: the cycle writes nothing, so what used to be asserted
    against fake DB calls is asserted against the `UpdateOutcome` it returns.
    `tests/test_app_update.py` covers the writes.
    """
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

    monkeypatch.setattr(ug, "run_resolution_check", fake_resolution)
    monkeypatch.setattr(ug, "run_update", fake_update)
    return state


async def visited(
    record: ForecastRecord,
) -> tuple[list[str], UpdateState, UpdateOutcome]:
    """Walk the cycle, reporting which nodes ran, the state, and what it concluded.

    Iterating a run yields a batch of scheduled tasks at a time, or an `EndMarker` when
    the graph finishes. A task names its node rather than being one, so the node name
    comes off `task.node_id` — which `BaseNode.get_node_id` defines as the class name,
    the same string this used to read off the node instance.
    """
    seen: list[str] = []
    st = UpdateState(record=record)
    async with ug.update_graph.iter(
        inputs=ug.CheckResolved(), state=st, deps=ForecastDeps()
    ) as run:
        async for item in run:
            if isinstance(item, EndMarker):
                break
            seen.extend(task.node_id for task in item)
    return seen, st, run.output


# ---------- resolution short-circuits ----------


async def test_resolved_forecast_never_reaches_the_update_step(stub):
    """The rule that used to live in a flagged_ids set, now an unreachable node."""
    stub["appears_resolved"] = True
    seen, _, _out = await visited(a_record())

    assert "ApplyBayes" not in seen
    assert "GuardUpdate" not in seen
    assert stub["update_calls"] == 0


async def test_resolved_forecast_is_flagged_not_closed(stub):
    """Never auto-resolves — an admin confirms."""
    stub["appears_resolved"] = True
    _, st, out = await visited(a_record())
    assert out.flagged_resolved is True
    assert out.updated is False


async def test_unresolved_forecast_proceeds_to_the_update(stub):
    seen, _, _out = await visited(a_record())
    assert seen[:3] == ["CheckResolved", "ApplyBayes", "GuardUpdate"]
    assert stub["update_calls"] == 1


# ---------- large-move verification ----------


async def test_large_move_routes_through_verification(stub):
    """FTX filing is a legitimate 0.20 -> 0.99 move; corroborate, don't cap."""
    stub["decision"] = a_decision(0.10, 0.95)
    seen, _, _out = await visited(a_record(0.10))

    assert "VerifyLargeMove" in seen
    assert stub["verify_calls"] == 1


async def test_verification_happens_at_most_once(stub):
    """Even if the verified decision is still a large move, no second pass."""
    stub["decision"] = a_decision(0.10, 0.95)
    stub["verify_decision"] = a_decision(0.10, 0.93)
    seen, _, _out = await visited(a_record(0.10))

    assert seen.count("VerifyLargeMove") == 1
    assert seen.count("GuardUpdate") == 2


async def test_a_survived_large_move_is_reported_as_an_update(stub):
    stub["decision"] = a_decision(0.10, 0.95)
    _, st, out = await visited(a_record(0.10))

    assert st.decision.verified_large_move is True
    assert (out.updated, out.new_probability) == (True, 0.95)


async def test_a_walked_back_large_move_reports_the_revised_value(stub):
    stub["decision"] = a_decision(0.10, 0.95)
    stub["verify_decision"] = a_decision(0.10, 0.30)
    _, st, out = await visited(a_record(0.10))

    assert (out.updated, out.new_probability) == (True, 0.30)


async def test_normal_move_skips_verification(stub):
    seen, _, _out = await visited(a_record())
    assert "VerifyLargeMove" not in seen
    assert stub["verify_calls"] == 0


async def test_large_move_threshold_is_configurable(stub, monkeypatch):
    monkeypatch.setenv("CHECK_LARGE_MOVE", "0.05")
    seen, _, _out = await visited(a_record())
    assert "VerifyLargeMove" in seen


# ---------- the write gate ----------


async def test_a_material_consistent_update_is_reported_as_an_update(stub):
    _, st, out = await visited(a_record())
    assert out.updated is True
    assert out.new_probability == 0.60
    assert out.reasoning == "r"


async def test_sub_threshold_move_is_treated_as_noise(stub):
    """Principle 10 — the 3-point gate that filters rephrasing of the same view."""
    stub["decision"] = a_decision(0.50, 0.51)
    _, st, out = await visited(a_record())

    assert out.updated is False


async def test_no_evidence_and_no_movement_is_a_clean_no_op(stub):
    stub["decision"] = a_decision(0.50, 0.50)
    _, st, out = await visited(a_record())

    assert out.updated is False
    assert st.violations == []


async def test_an_internally_inconsistent_update_is_refused(stub):
    """Principle 11 — evidence points up, the number went down. Refuse the write."""
    stub["decision"] = UpdateDecision(
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.1)],
        prior=0.50,
        posterior=0.30,
        reasoning="contradicts itself",
    )
    _, st, out = await visited(a_record())

    assert out.updated is False
    assert 11 in {v.principle for v in st.violations}


# ---------- diagram ----------


def test_mermaid_shows_both_branches():
    """A node with two possible successors now renders through an explicit
    `<<choice>>` node, so `CheckResolved --> ApplyBayes` is drawn in two hops."""
    code = ug.update_mermaid()

    assert "[*] --> CheckResolved" in code
    assert "CheckResolved --> decision" in code
    assert "decision --> ApplyBayes" in code  # not resolved
    assert "decision --> [*]" in code  # resolved, straight to the end

    assert "GuardUpdate --> decision_2" in code
    assert "decision_2 --> VerifyLargeMove" in code
    assert "VerifyLargeMove --> GuardUpdate" in code
