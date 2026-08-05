"""Tests for the per-column fan-out in the two research rows.

Decompose fixes a grid: rows are stages, columns are sub-questions. Each research row
runs one agent per column concurrently and merges at a barrier. Nothing here starts a
real agent — the cell function is stubbed, so what is verified is the shape of the
fan-out and the merge, not what a model would say.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded

from superforecaster import checks
from superforecaster.agents import inside_view as iv
from superforecaster.agents import outside_view as ov
from superforecaster.deps import ForecastDeps
from superforecaster.models import (
    ALL_BIASES,
    Adjustment,
    BiasCheck,
    Decomposition,
    ForecastInput,
    OutsideView,
    Reflection,
    SourceRef,
    SubClaimAdjustments,
    SubClaimBaseRates,
    SubPrediction,
)
from tests.test_checks import graded, ref

pytestmark = pytest.mark.anyio


async def _done(value):
    """Wrap a value so a plain lambda can stand in for an async function."""
    return value


def forecast_input(**kw) -> ForecastInput:
    defaults = {
        "question": "Will OpenAI go public in 2026?",
        "resolution_criteria": "An IPO completes on a US national exchange",
        "resolution_date": "2026-12-31T00:00:00Z",
        "category": "finance",
        "max_iterations": 2,
    }
    return ForecastInput(**{**defaults, **kw})


def decomposition(knowabilities: list[str], rule: str = "conjunction") -> Decomposition:
    d = Decomposition(
        sub_claims=[
            SubPrediction(
                question=f"part {n}",
                probability=0.5,
                rationale="because",
                knowability=k,
            )
            for n, k in enumerate(knowabilities, 1)
        ],
        chain_rule=rule,
        chain_note="stated",
    )
    return d.model_copy(
        update={
            "sub_claims": [
                s.model_copy(update={"id": f"sc{i}"})
                for i, s in enumerate(d.sub_claims, 1)
            ]
        }
    )


def cell_result(rate: float, disagreement: str = "") -> SubClaimBaseRates:
    return SubClaimBaseRates(
        reference_classes=[
            ref("broad", rate, weight=1.0),
            ref("narrow", rate, weight=1.0),
        ],
        disagreement=disagreement,
    )


# ---------- one cell per researchable column ----------


async def test_one_cell_runs_per_researchable_column(monkeypatch):
    seen: list[str] = []

    async def fake_cell(input, d, sub_claim, deps):
        seen.append(sub_claim.id)
        return cell_result(0.5)

    monkeypatch.setattr(ov, "run_base_rate_cell", fake_cell)
    d = decomposition(["researchable", "judgment", "researchable"])
    await ov.run_outside_view(forecast_input(), d, ForecastDeps())

    # sc2 is judgment: by its own label there is no base rate to look up, so no cell.
    assert seen == ["sc1", "sc3"]


async def test_each_cell_sees_only_its_own_column(monkeypatch):
    got: dict[str, str] = {}

    async def fake_cell(input, d, sub_claim, deps):
        got[sub_claim.id] = deps.sub_claim
        return cell_result(0.5)

    monkeypatch.setattr(ov, "run_base_rate_cell", fake_cell)
    d = decomposition(["researchable"] * 3)
    await ov.run_outside_view(forecast_input(), d, ForecastDeps())

    assert got == {"sc1": "sc1", "sc2": "sc2", "sc3": "sc3"}


async def test_the_cells_actually_run_concurrently(monkeypatch):
    """The barrier is `asyncio.gather`, not a loop. Serial cells would take 3 ticks."""
    running = 0
    peak = 0

    async def fake_cell(input, d, sub_claim, deps):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return cell_result(0.5)

    monkeypatch.setattr(ov, "run_base_rate_cell", fake_cell)
    d = decomposition(["researchable"] * 3)
    await ov.run_outside_view(forecast_input(), d, ForecastDeps())

    assert peak == 3


# ---------- the merge ----------


async def test_every_merged_class_names_exactly_one_column(monkeypatch):
    """Stamped by code, unconditionally — even over whatever the model volunteered.

    A cell researched exactly one sub-question. Letting it name a different one would
    re-open the linkage hole `check_linkage` closes.
    """

    async def lying_cell(input, d, sub_claim, deps):
        out = cell_result(0.5)
        out.reference_classes[0].sub_claim_ids = ["sc99", "sc1"]
        out.reference_classes[1].sub_claim_ids = []
        return out

    monkeypatch.setattr(ov, "run_base_rate_cell", lying_cell)
    d = decomposition(["researchable", "researchable", "judgment"])
    o = await ov.run_outside_view(forecast_input(), d, ForecastDeps())

    assert all(len(rc.sub_claim_ids) == 1 for rc in o.reference_classes)
    assert {rc.sub_claim_ids[0] for rc in o.reference_classes} == {"sc1", "sc2"}
    assert checks.check_linkage.__name__  # the ids above are all real


async def test_the_anchor_is_the_chain_not_the_mean(monkeypatch):
    rates = {"sc1": 0.55, "sc2": 0.70, "sc3": 0.60}

    async def fake_cell(input, d, sub_claim, deps):
        return cell_result(rates[sub_claim.id])

    monkeypatch.setattr(ov, "run_base_rate_cell", fake_cell)
    d = decomposition(["researchable"] * 3, rule="conjunction")
    o = await ov.run_outside_view(forecast_input(), d, ForecastDeps())

    assert o.aggregate_base_rate == pytest.approx(0.55 * 0.70 * 0.60)
    # And the check re-derives it with the same function, so it agrees.
    assert checks.check_aggregation(o, d) is None


async def test_per_column_disagreement_is_carried_with_its_id(monkeypatch):
    async def fake_cell(input, d, sub_claim, deps):
        return cell_result(0.5, "the narrow class excludes hostile bids")

    monkeypatch.setattr(ov, "run_base_rate_cell", fake_cell)
    d = decomposition(["researchable", "researchable", "judgment"])
    o = await ov.run_outside_view(forecast_input(), d, ForecastDeps())

    assert "sc1: the narrow class" in o.disagreement
    assert "sc2: the narrow class" in o.disagreement


# ---------- a failing cell ----------


async def test_one_failed_cell_does_not_take_the_row_down(monkeypatch):
    async def flaky_cell(input, d, sub_claim, deps):
        if sub_claim.id == "sc2":
            raise RuntimeError("search provider is down")
        return cell_result(0.5)

    monkeypatch.setattr(ov, "run_base_rate_cell", flaky_cell)
    d = decomposition(["researchable"] * 3)
    o = await ov.run_outside_view(forecast_input(), d, ForecastDeps())

    assert {rc.sub_claim_ids[0] for rc in o.reference_classes} == {"sc1", "sc3"}
    # sc2 contributes its own working estimate rather than vanishing from the product.
    rows = {r["id"]: r for r in checks.chain_inputs(d, o)}
    assert rows["sc2"]["source"] == "estimated"
    assert o.aggregate_base_rate == pytest.approx(0.5 * 0.5 * 0.5)


async def test_every_cell_failing_raises(monkeypatch):
    """Nothing was researched at all — that is ADR 28's checkpoint case, not a degraded
    column."""

    async def dead_cell(input, d, sub_claim, deps):
        raise RuntimeError("network down")

    monkeypatch.setattr(ov, "run_base_rate_cell", dead_cell)
    d = decomposition(["researchable"] * 3)

    with pytest.raises(RuntimeError, match="network down"):
        await ov.run_outside_view(forecast_input(), d, ForecastDeps())


# ---------- sources ----------


async def test_each_cell_gets_a_private_source_list_merged_after_the_barrier(monkeypatch):
    """`observability` detects new sources by slicing the tail off this list. Shared, two
    concurrent cells hand each other's sources to whoever asks second."""

    async def sourcing_cell(input, d, sub_claim, deps):
        assert deps.sources_seen == []
        deps.sources_seen.append(
            SourceRef(
                url=f"https://x.test/{sub_claim.id}",
                domain="x.test",
                title="t",
                tool="search_web",
            )
        )
        return cell_result(0.5)

    monkeypatch.setattr(ov, "run_base_rate_cell", sourcing_cell)
    parent = ForecastDeps()
    d = decomposition(["researchable", "researchable", "judgment"])
    await ov.run_outside_view(forecast_input(), d, parent)

    assert sorted(s.url for s in parent.sources_seen) == [
        "https://x.test/sc1",
        "https://x.test/sc2",
    ]


# ---------- the fallback ----------


async def test_nothing_researchable_falls_back_to_the_whole_question(monkeypatch):
    called = {}

    async def fake_whole(input, d, deps):
        called["yes"] = True
        return ov.OutsideView(
            reference_classes=[ref("a", 0.2), ref("b", 0.24)],
            aggregate_base_rate=0.22,
        )

    monkeypatch.setattr(ov, "_whole_question_cell", fake_whole)
    d = decomposition(["judgment"] * 3)
    o = await ov.run_outside_view(forecast_input(), d, ForecastDeps())

    assert called == {"yes": True}
    assert o.aggregate_base_rate == pytest.approx(0.22)


# ---------- the inside-view row ----------


def an_inside_cell(magnitude: float = 0.05, steel_man: str = "the case against") -> SubClaimAdjustments:
    return SubClaimAdjustments(
        adjustments=[
            Adjustment(
                evidence="something specific",
                direction="up",
                magnitude=magnitude,
                flip_test="I would move down",
                sources=[graded()],
            )
        ],
        steel_man=steel_man,
        what_would_change_my_mind="a filing",
    )


def a_reflection() -> Reflection:
    return Reflection(
        steel_man="the whole-question case against",
        what_would_change_my_mind="an S-1",
        bias_checks=[
            BiasCheck(bias=b, assessment=f"checked {b}") for b in ALL_BIASES
        ],
    )


def researched_outside(columns: dict[str, float]) -> OutsideView:
    classes = [
        ref(f"{cid} lens {n}", rate, weight=1.0).model_copy(
            update={"sub_claim_ids": [cid]}
        )
        for cid, rate in columns.items()
        for n in (1, 2)
    ]
    return OutsideView(reference_classes=classes, aggregate_base_rate=0.2)


async def test_inside_cells_run_only_for_columns_with_a_base_rate(monkeypatch):
    """No reference class means nothing to adjust *from*, which is P5's premise."""
    seen: list[str] = []

    async def fake_cell(input, sub_claim, outside, deps):
        seen.append(sub_claim.id)
        return an_inside_cell()

    monkeypatch.setattr(iv, "run_inside_view_cell", fake_cell)
    monkeypatch.setattr(iv, "run_reflect", lambda *a, **k: _done(a_reflection()))

    d = decomposition(["researchable"] * 3)
    o = researched_outside({"sc1": 0.5, "sc3": 0.4})
    await iv.run_inside_view(forecast_input(), d, o, ForecastDeps())

    assert seen == ["sc1", "sc3"]


async def test_each_inside_cell_is_seeded_with_its_own_columns_rate(monkeypatch):
    """The substantive change on this row: not the whole-question anchor."""
    seeds: dict[str, float] = {}

    async def fake_cell(input, sub_claim, outside, deps):
        seeds[sub_claim.id] = checks.sub_claim_rate(sub_claim.id, outside)
        return an_inside_cell()

    monkeypatch.setattr(iv, "run_inside_view_cell", fake_cell)
    monkeypatch.setattr(iv, "run_reflect", lambda *a, **k: _done(a_reflection()))

    d = decomposition(["researchable"] * 3)
    o = researched_outside({"sc1": 0.55, "sc2": 0.70, "sc3": 0.60})
    await iv.run_inside_view(forecast_input(), d, o, ForecastDeps())

    assert seeds == {"sc1": 0.55, "sc2": 0.70, "sc3": 0.60}
    assert o.aggregate_base_rate == 0.2  # deliberately different from all three


async def test_every_merged_adjustment_names_its_column(monkeypatch):
    async def lying_cell(input, sub_claim, outside, deps):
        out = an_inside_cell()
        out.adjustments[0].sub_claim_ids = ["sc99"]
        return out

    monkeypatch.setattr(iv, "run_inside_view_cell", lying_cell)
    monkeypatch.setattr(iv, "run_reflect", lambda *a, **k: _done(a_reflection()))

    d = decomposition(["researchable"] * 3)
    o = researched_outside({"sc1": 0.5, "sc2": 0.4})
    result = await iv.run_inside_view(forecast_input(), d, o, ForecastDeps())

    assert [a.sub_claim_ids for a in result.adjustments] == [["sc1"], ["sc2"]]


async def test_the_reflect_pass_supplies_the_whole_question_fields(monkeypatch):
    """Five columns give twenty-five bias checks; InsideView wants exactly five."""
    async def fake_cell(input, sub_claim, outside, deps):
        return an_inside_cell()

    monkeypatch.setattr(iv, "run_inside_view_cell", fake_cell)
    monkeypatch.setattr(iv, "run_reflect", lambda *a, **k: _done(a_reflection()))

    d = decomposition(["researchable"] * 3)
    o = researched_outside({"sc1": 0.5, "sc2": 0.4, "sc3": 0.6})
    result = await iv.run_inside_view(forecast_input(), d, o, ForecastDeps())

    assert len(result.bias_checks) == 5
    assert result.steel_man == "the whole-question case against"


async def test_reflect_sees_every_columns_adjustments(monkeypatch):
    """`check_disconfirming` fails when every adjustment points the same direction. No
    single column can evaluate that — this pass is where it becomes visible."""
    captured: dict = {}

    async def fake_cell(input, sub_claim, outside, deps):
        return an_inside_cell(steel_man=f"against {sub_claim.id}")

    async def spying_reflect(input, d, outside, adjustments, steel_mans, deps):
        captured["n"] = len(adjustments)
        captured["steel_mans"] = dict(steel_mans)
        return a_reflection()

    monkeypatch.setattr(iv, "run_inside_view_cell", fake_cell)
    monkeypatch.setattr(iv, "run_reflect", spying_reflect)

    d = decomposition(["researchable"] * 3)
    o = researched_outside({"sc1": 0.5, "sc2": 0.4, "sc3": 0.6})
    await iv.run_inside_view(forecast_input(), d, o, ForecastDeps())

    assert captured["n"] == 3
    assert captured["steel_mans"] == {
        "sc1": "against sc1",
        "sc2": "against sc2",
        "sc3": "against sc3",
    }


async def test_one_failed_inside_cell_does_not_take_the_row_down(monkeypatch):
    async def flaky_cell(input, sub_claim, outside, deps):
        if sub_claim.id == "sc2":
            raise RuntimeError("search provider is down")
        return an_inside_cell()

    monkeypatch.setattr(iv, "run_inside_view_cell", flaky_cell)
    monkeypatch.setattr(iv, "run_reflect", lambda *a, **k: _done(a_reflection()))

    d = decomposition(["researchable"] * 3)
    o = researched_outside({"sc1": 0.5, "sc2": 0.4, "sc3": 0.6})
    result = await iv.run_inside_view(forecast_input(), d, o, ForecastDeps())

    assert [a.sub_claim_ids[0] for a in result.adjustments] == ["sc1", "sc3"]


# ---------- the budget is per cell ----------


def test_each_cell_gets_its_own_budget():
    deps = ForecastDeps()
    a = ov.cell_deps(deps, "sc1", max_iterations=5)
    b = ov.cell_deps(deps, "sc2", max_iterations=5)

    assert a.budget.sub_claim == "sc1"
    assert b.budget.sub_claim == "sc2"
    assert a.budget is not b.budget
    assert a.sources_seen is not b.sources_seen
    assert a.budget.soft_depth == 5 and a.budget.hard_depth == 8


# ---------- degrading a cell instead of killing the run ----------


async def test_a_cell_that_blows_its_wall_does_not_kill_the_row(monkeypatch):
    emitted: list[tuple[str, dict, str | None]] = []

    async def greedy_cell(input, d, sub_claim, deps):
        if sub_claim.id == "sc2":
            raise UsageLimitExceeded("tool calls exceeded")
        return cell_result(0.5)

    monkeypatch.setattr(ov, "run_base_rate_cell", greedy_cell)
    d = decomposition(["researchable"] * 3)
    deps = ForecastDeps(emit=lambda t, p, sc=None: emitted.append((t, p, sc)))
    o = await ov.run_outside_view(forecast_input(), d, deps)

    assert {rc.sub_claim_ids[0] for rc in o.reference_classes} == {"sc1", "sc3"}
    assert [(t, sc) for t, _p, sc in emitted] == [("exhausted", "sc2")]
    # sc2 falls back to its own working estimate rather than vanishing from the product.
    assert o.aggregate_base_rate == pytest.approx(0.5 * 0.5 * 0.5)


async def test_every_cell_blowing_its_wall_raises_with_the_depths_spent(monkeypatch):
    """The one case a degraded cell cannot absorb: OutsideView needs two classes and
    there are none. ADR 28's resume-with-a-higher-depth is the right answer."""

    async def greedy_cell(input, d, sub_claim, deps):
        deps.budget.used = deps.budget.hard_depth
        raise UsageLimitExceeded("tool calls exceeded")

    monkeypatch.setattr(ov, "run_base_rate_cell", greedy_cell)
    d = decomposition(["researchable"] * 3)

    with pytest.raises(UsageLimitExceeded, match="every column exhausted"):
        await ov.run_outside_view(forecast_input(), d, ForecastDeps())


async def test_an_inside_cell_that_blows_its_wall_does_not_kill_the_row(monkeypatch):
    async def greedy_cell(input, sub_claim, outside, deps):
        if sub_claim.id == "sc2":
            raise UsageLimitExceeded("tool calls exceeded")
        return an_inside_cell()

    monkeypatch.setattr(iv, "run_inside_view_cell", greedy_cell)
    monkeypatch.setattr(iv, "run_reflect", lambda *a, **k: _done(a_reflection()))

    d = decomposition(["researchable"] * 3)
    o = researched_outside({"sc1": 0.5, "sc2": 0.4, "sc3": 0.6})
    result = await iv.run_inside_view(forecast_input(), d, o, ForecastDeps())

    assert [a.sub_claim_ids[0] for a in result.adjustments] == ["sc1", "sc3"]
