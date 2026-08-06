"""Tests for the per-column fan-out in the two research rows.

Decompose fixes the columns; each research row runs one agent per column and merges at a
barrier. That fan-out is now a `.map()` edge and a join in `graphs.forecast` rather than
an `asyncio.gather` inside the agent module, so these tests drive the graph and read the
state and events it produced. Nothing here starts a real agent — the *cell* function is
stubbed, which is the seam the map runs over.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded

from superforecaster import checks
from superforecaster.deps import ForecastDeps
from superforecaster.graphs import forecast as fg
from superforecaster.graphs.state import ForecastState
from superforecaster.models import (
    ALL_BIASES,
    BiasCheck,
    Decomposition,
    ForecastInput,
    Reflection,
    SourceRef,
    SubClaimAdjustments,
    SubClaimBaseRates,
    SubClaimLenses,
    SubPrediction,
)
from tests.test_checks import graded, ref
from tests.test_graph_forecast import a_forecast, a_lens

pytestmark = pytest.mark.anyio


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


def cell_result(rate: float, name: str = "lens", disagreement: str = "") -> SubClaimBaseRates:
    """One measured population, at the given rate."""
    return SubClaimBaseRates(lens=ref(name, rate, weight=1.0), disagreement=disagreement)


def adjustments(n: int = 1, lens_name: str = "lens") -> SubClaimAdjustments:
    from tests.test_checks import adjustment

    return SubClaimAdjustments(
        lens_name=lens_name,
        adjustments=[adjustment("up", 0.02) for _ in range(n)],
        steel_man="the case against",
        what_would_change_my_mind="a filing",
    )


def a_reflection() -> Reflection:
    return Reflection(
        steel_man="whole-question case against",
        what_would_change_my_mind="a whole-question observation",
        bias_checks=[BiasCheck(bias=b, assessment=f"considered {b}") for b in ALL_BIASES],
    )


async def run_graph(
    monkeypatch,
    d: Decomposition,
    *,
    choose_lenses=None,
    base_rate_cell=None,
    inside_cell=None,
    reflect=None,
    deps: ForecastDeps | None = None,
    input: ForecastInput | None = None,
) -> tuple[ForecastState, list[tuple]]:
    """Run the whole graph with every agent stubbed, and hand back what it built.

    Returns the mutated state plus every event emitted, which between them cover
    everything these tests need to assert: what each cell was asked, what the barrier
    merged, and what the row told the UI.
    """

    async def default_choose(input, decomposition, sub_claim, deps):
        return SubClaimLenses(lenses=[a_lens(f"{sub_claim.id}-lens")])

    async def default_base_rate(input, sub_claim, lens, deps):
        return cell_result(0.2, lens.name)

    async def default_inside(input, sub_claim, lens, already_controlled_for, deps):
        return adjustments(lens_name=lens.name)

    async def default_reflect(input, d_, o, adjs, steel_mans, deps):
        return a_reflection()

    async def stub_decompose(input, deps):
        return d

    async def stub_synthesize(input, d_, o, i, violations, deps):
        return a_forecast()

    monkeypatch.setattr(fg, "run_decompose", stub_decompose)
    monkeypatch.setattr(fg, "run_choose_lenses", choose_lenses or default_choose)
    monkeypatch.setattr(fg, "run_research_lens", base_rate_cell or default_base_rate)
    monkeypatch.setattr(fg, "run_adjust_lens", inside_cell or default_inside)
    monkeypatch.setattr(fg, "run_reflect", reflect or default_reflect)
    monkeypatch.setattr(fg, "run_synthesize", stub_synthesize)

    events: list[tuple] = []
    deps = deps or ForecastDeps()
    deps.emit = lambda t, p, sc=None: events.append((t, p, sc))
    state = ForecastState(input=input or forecast_input())
    await fg.forecast_graph.run(state=state, deps=deps)
    return state, events


# ---------- one cell per researchable column ----------


async def test_one_cell_runs_per_researchable_column(monkeypatch):
    """A `judgment` column has no base rate to look up, so no cell runs for it."""
    seen: list[str] = []

    async def cell(input, sub_claim, lens, deps):
        seen.append(sub_claim.id)
        return cell_result(0.2, lens.name)

    d = decomposition(["researchable", "judgment", "researchable"])
    await run_graph(monkeypatch, d, base_rate_cell=cell)

    assert seen == ["sc1", "sc3"]


async def test_each_cell_sees_only_its_own_column(monkeypatch):
    asked: dict[str, str] = {}

    async def cell(input, sub_claim, lens, deps):
        asked[sub_claim.id] = sub_claim.question
        return cell_result(0.2, lens.name)

    d = decomposition(["researchable", "researchable", "judgment"])
    await run_graph(monkeypatch, d, base_rate_cell=cell)

    assert asked == {"sc1": "part 1", "sc2": "part 2"}


async def test_the_cells_actually_run_concurrently(monkeypatch):
    """The map is a fan-out, not a loop. Every cell starts before any cell finishes,
    which a serial implementation cannot produce."""
    order: list[str] = []

    async def cell(input, sub_claim, lens, deps):
        order.append(f"start:{sub_claim.id}")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        order.append(f"end:{sub_claim.id}")
        return cell_result(0.2, lens.name)

    d = decomposition(["researchable"] * 3)
    await run_graph(monkeypatch, d, base_rate_cell=cell)

    last_start = max(i for i, e in enumerate(order) if e.startswith("start:"))
    first_end = min(i for i, e in enumerate(order) if e.startswith("end:"))
    assert last_start < first_end


# ---------- the merge ----------


async def test_every_merged_class_names_exactly_one_column(monkeypatch):
    """Stamped by code, not volunteered by the model: a cell researched one column, and
    a link it invented could point anywhere."""
    d = decomposition(["researchable", "researchable", "judgment"])
    state, _ = await run_graph(monkeypatch, d)

    assert state.outside is not None
    for rc in state.outside.lenses:
        assert len(rc.sub_claim_ids) == 1
    assert {rc.sub_claim_ids[0] for rc in state.outside.lenses} == {
        "sc1",
        "sc2",
    }


async def test_the_anchor_is_the_chain_not_the_mean(monkeypatch):
    """Three columns at 0.5 in a conjunction is 0.125, not the 0.5 mean.

    The judgment column contributes its own working estimate to the chain even though no
    cell researched it — which is exactly why it still gets a card.
    """

    async def cell(input, sub_claim, lens, deps):
        return cell_result(0.5, lens.name)

    d = decomposition(["researchable", "researchable", "judgment"], rule="conjunction")
    state, _ = await run_graph(monkeypatch, d, base_rate_cell=cell)

    assert state.outside is not None
    assert state.outside.aggregate_base_rate == pytest.approx(0.125, abs=0.01)


async def test_per_column_disagreement_is_carried_with_its_id(monkeypatch):
    async def cell(input, sub_claim, lens, deps):
        return cell_result(0.2, lens.name, disagreement=f"{sub_claim.id} is contested")

    d = decomposition(["researchable", "researchable", "judgment"])
    state, _ = await run_graph(monkeypatch, d, base_rate_cell=cell)

    assert state.outside is not None
    assert "sc1: sc1 is contested" in state.outside.disagreement
    assert "sc2: sc2 is contested" in state.outside.disagreement


# ---------- a failing cell ----------


async def test_one_failed_cell_does_not_take_the_row_down(monkeypatch):
    """A sibling raising must not cancel the rest of the row — the columns that did
    finish still contribute, and the failed one falls back to its own estimate."""

    async def cell(input, sub_claim, lens, deps):
        if sub_claim.id == "sc1":
            raise RuntimeError("provider exploded")
        return cell_result(0.2, lens.name)

    d = decomposition(["researchable", "researchable", "judgment"])
    state, _ = await run_graph(monkeypatch, d, base_rate_cell=cell)

    assert state.outside is not None
    assert {rc.sub_claim_ids[0] for rc in state.outside.lenses} == {"sc2"}


async def test_every_cell_failing_raises(monkeypatch):
    """Nothing was researched at all. There is no anchor to invent, and inventing one
    would be worse than an error."""

    async def cell(input, sub_claim, lens, deps):
        raise RuntimeError("network down")

    d = decomposition(["researchable", "researchable", "judgment"])
    with pytest.raises(Exception, match="every lens failed to measure"):
        await run_graph(monkeypatch, d, base_rate_cell=cell)


# ---------- sources ----------


async def test_each_cell_gets_a_private_source_list_merged_after_the_barrier(monkeypatch):
    """`observability` detects new sources by slicing the tail off `sources_seen`, so
    two cells appending to one list would hand each cell the other's sources."""

    async def cell(input, sub_claim, lens, deps):
        assert deps.sources_seen == [], "a cell started with a dirty source list"
        deps.sources_seen.append(
            SourceRef(url=f"https://x/{sub_claim.id}", title="t", query="q", tool="search_web")
        )
        return cell_result(0.2, lens.name)

    parent = ForecastDeps()
    d = decomposition(["researchable", "researchable", "judgment"])
    state, _ = await run_graph(monkeypatch, d, base_rate_cell=cell, deps=parent)

    assert sorted(s.url for s in parent.sources_seen) == [
        "https://x/sc1",
        "https://x/sc2",
    ]
    assert sorted(s.url for s in state.sources_seen) == [
        "https://x/sc1",
        "https://x/sc2",
    ]


# ---------- the fallback ----------


async def test_nothing_researchable_falls_back_to_the_whole_question(monkeypatch):
    """No column was labelled researchable, so no cell ran. The row still has to
    produce an outside view rather than crashing."""
    called = {"fallback": 0}

    async def fallback(input, decomposition, deps, errors):
        called["fallback"] += 1
        from superforecaster.models import OutsideView

        return OutsideView(
            lenses=[ref("whole", 0.3), ref("question", 0.3)],
            aggregate_base_rate=0.3,
            disagreement="",
        )

    async def inside_fallback(input, outside, deps, errors):
        # The fallback OutsideView names no column, so no inside cell runs either and
        # that row falls back too. Stubbed so this test stays about the outside row.
        from tests.test_checks import adjustment

        return [adjustment("up", 0.02)], {"whole question": "the case against"}

    monkeypatch.setattr(fg, "whole_question_outside", fallback)
    monkeypatch.setattr(fg, "whole_question_adjustments", inside_fallback)
    d = decomposition(["judgment", "judgment", "judgment"])
    state, _ = await run_graph(monkeypatch, d)

    assert called["fallback"] == 1
    assert state.outside is not None
    assert state.outside.aggregate_base_rate == 0.3


# ---------- the inside-view row ----------


async def test_inside_cells_run_only_for_columns_with_a_base_rate(monkeypatch):
    """A column with nothing researched has no base rate to adjust *from*, which is
    principle 5's premise. Running a cell on it would be an absolute estimate wearing a
    delta's clothes."""
    seen: list[str] = []

    async def base_rate(input, sub_claim, lens, deps):
        if sub_claim.id == "sc2":
            raise RuntimeError("no classes for this one")
        return cell_result(0.2, lens.name)

    async def inside(input, sub_claim, lens, acf, deps):
        seen.append(sub_claim.id)
        return adjustments(lens_name=lens.name)

    d = decomposition(["researchable", "researchable", "judgment"])
    await run_graph(monkeypatch, d, base_rate_cell=base_rate, inside_cell=inside)

    assert seen == ["sc1"]


async def test_each_inside_cell_is_seeded_with_its_own_columns_rate(monkeypatch):
    """The global anchor is the wrong reference point for one part of a decomposed
    question."""
    rates: dict[str, float] = {}

    async def base_rate(input, sub_claim, lens, deps):
        return cell_result(0.2 if sub_claim.id == 'sc1' else 0.6, lens.name)

    async def inside(input, sub_claim, lens, acf, deps):
        rates[sub_claim.id] = checks.lens_rate(lens)
        return adjustments(lens_name=lens.name)

    d = decomposition(["researchable", "researchable", "judgment"])
    await run_graph(monkeypatch, d, base_rate_cell=base_rate, inside_cell=inside)

    assert rates == {"sc1": pytest.approx(0.2), "sc2": pytest.approx(0.6)}


async def test_every_merged_adjustment_names_its_column(monkeypatch):
    d = decomposition(["researchable", "researchable", "judgment"])
    state, _ = await run_graph(monkeypatch, d)

    assert state.inside is not None
    for a in state.inside.adjustments:
        assert len(a.sub_claim_ids) == 1
    assert {a.sub_claim_ids[0] for a in state.inside.adjustments} == {"sc1", "sc2"}


async def test_the_reflect_pass_supplies_the_whole_question_fields(monkeypatch):
    """Its own step now. The steel man on the InsideView is the whole-question one the
    reflect agent wrote, not any single column's."""
    d = decomposition(["researchable", "researchable", "judgment"])
    state, _ = await run_graph(monkeypatch, d)

    assert state.inside is not None
    assert state.inside.steel_man == "whole-question case against"
    assert state.inside.what_would_change_my_mind == "a whole-question observation"


async def test_reflect_sees_every_columns_adjustments(monkeypatch):
    """Which is exactly why it cannot be asked of one lens, and why it is a step of its
    own rather than a tail call inside a cell. Keyed by lens, since a sub-question can
    have several and each argues its own case."""
    seen = {}

    async def reflect(input, d_, o, adjs, steel_mans, deps):
        seen["adjustments"] = len(adjs)
        seen["steel_mans"] = sorted(steel_mans)
        return a_reflection()

    d = decomposition(["researchable", "researchable", "judgment"])
    await run_graph(monkeypatch, d, inside_cell=lambda *a: _two(), reflect=reflect)

    assert seen["adjustments"] == 4
    assert seen["steel_mans"] == ["sc1-lens", "sc2-lens"]


async def _two():
    return adjustments(2)


async def test_reflect_is_its_own_stage(monkeypatch):
    """It is an agent run, so it gets a stage of its own in the UI and its own step in
    the durable workflow."""
    d = decomposition(["researchable", "judgment", "judgment"])
    _, events = await run_graph(monkeypatch, d)

    stages = [p["stage"] for t, p, _ in events if t == "stage"]
    assert "reflect" in stages
    assert stages.index("inside") < stages.index("reflect") < stages.index("synth")


async def test_one_failed_inside_cell_does_not_take_the_row_down(monkeypatch):
    async def inside(input, sub_claim, lens, acf, deps):
        if sub_claim.id == "sc1":
            raise RuntimeError("provider exploded")
        return adjustments(lens_name=lens.name)

    d = decomposition(["researchable", "researchable", "judgment"])
    state, _ = await run_graph(monkeypatch, d, inside_cell=inside)

    assert state.inside is not None
    assert {a.sub_claim_ids[0] for a in state.inside.adjustments} == {"sc2"}


# ---------- the budget is per cell ----------


def test_each_cell_gets_its_own_budget():
    from superforecaster.agents.outside_view import cell_deps

    parent = ForecastDeps()
    a = cell_deps(parent, "sc1", 2)
    b = cell_deps(parent, "sc2", 2)

    assert a.budget is not None and b.budget is not None
    assert a.budget.sub_claim == "sc1"
    assert b.budget.sub_claim == "sc2"
    assert a.budget is not b.budget
    assert a.sources_seen is not b.sources_seen


# ---------- degrading a cell instead of killing the run ----------


async def test_a_cell_that_blows_its_wall_does_not_kill_the_row(monkeypatch):
    """One greedy column must not cost the others their work — the whole point of
    moving the budget from the row to the cell."""

    async def cell(input, sub_claim, lens, deps):
        if sub_claim.id == "sc1":
            raise UsageLimitExceeded("out of searches")
        return cell_result(0.2, lens.name)

    d = decomposition(["researchable", "researchable", "judgment"])
    state, events = await run_graph(monkeypatch, d, base_rate_cell=cell)

    assert state.outside is not None
    assert {rc.sub_claim_ids[0] for rc in state.outside.lenses} == {"sc2"}
    assert any(t == "exhausted" for t, _, _ in events)


async def test_every_cell_blowing_its_wall_says_to_raise_the_depth(monkeypatch):
    async def cell(input, sub_claim, lens, deps):
        raise UsageLimitExceeded("out of searches")

    d = decomposition(["researchable", "researchable", "judgment"])
    with pytest.raises(UsageLimitExceeded, match="higher search depth"):
        await run_graph(monkeypatch, d, base_rate_cell=cell)


async def test_an_inside_cell_that_blows_its_wall_does_not_kill_the_row(monkeypatch):
    async def inside(input, sub_claim, lens, acf, deps):
        if sub_claim.id == "sc1":
            raise UsageLimitExceeded("out of searches")
        return adjustments(lens_name=lens.name)

    d = decomposition(["researchable", "researchable", "judgment"])
    state, _ = await run_graph(monkeypatch, d, inside_cell=inside)

    assert state.inside is not None
    assert {a.sub_claim_ids[0] for a in state.inside.adjustments} == {"sc2"}
