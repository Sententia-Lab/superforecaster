"""Tests for the forecast graph's wiring.

These test routing, not agent quality. The agents are stubbed out — what is being
verified is that the graph visits nodes in the right order and takes the retry edge
the right number of times.

Stage order is the point of the whole exercise: principle 4 ("outside view first") is
enforced by the base-rate row preceding the inside-view row in the graph rather than by
asking a model nicely. If that edge ever reverses, this file is what catches it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from superforecaster.graphs import forecast as fg
from superforecaster.graphs.state import ForecastDeps, ForecastState
from superforecaster.models import (
    CheckViolation,
    Decomposition,
    Forecast,
    ForecastInput,
    InsideView,
    Lens,
    OutsideView,
    Reflection,
    ResearchSummary,
    SubClaimAdjustments,
    SubClaimBaseRates,
    SubClaimLenses,
)
from tests.test_checks import (  # reuse the model factories
    adjustment,
    all_bias_checks,
    ref,
    sub,
)


def forecast_input() -> ForecastInput:
    return ForecastInput(
        question="Will A acquire B?",
        resolution_criteria="Deal closes",
        resolution_date=datetime(2027, 1, 1, tzinfo=timezone.utc),
        category="business",
    )


def a_decomposition() -> Decomposition:
    """Ids included — `run_decompose` stamps them, and these tests bypass it."""
    return Decomposition(
        sub_claims=[
            sub().model_copy(update={"id": f"sc{i}"}) for i in (1, 2, 3)
        ],
        chain_note="multiply",
    )


def an_outside_view() -> OutsideView:
    return OutsideView(
        lenses=[ref("a", 0.22), ref("b", 0.22)],
        aggregate_base_rate=0.22,
        disagreement="",
    )


def a_lens(name: str = "L", weight: float = 1.0) -> Lens:
    """A chosen population, before anything has measured it."""
    return Lens(
        name=name,
        population=f"cases comparable to {name}",
        why_it_fits="it is the population this sub-question belongs to",
        weight=weight,
        weight_rationale="the closest available population",
    )


def an_inside_view() -> InsideView:
    return InsideView(
        adjustments=[adjustment("up", 0.10), adjustment("down", 0.04)],
        steel_man="regulators could block it",
        what_would_change_my_mind="a second request",
        bias_checks=all_bias_checks(),
    )


def a_forecast(probability: float = 0.28) -> Forecast:
    return Forecast(
        question="Will A acquire B?",
        resolution_criteria="Deal closes",
        resolution_date=datetime(2027, 1, 1, tzinfo=timezone.utc),
        category="business",
        probability=probability,
        # Ids carried through from the decomposition — synthesis is asked to do exactly
        # this, and `check_linkage` fails the forecast when it does not.
        decompositions=a_decomposition().sub_claims,
        research=ResearchSummary(),
        reasoning="base rate then adjustments",
    )


@pytest.fixture
def stub_agents(monkeypatch):
    """Replace every agent with a canned, methodology-clean result.

    The research rows are stubbed at the *cell* — one agent per sub-question — because
    that is the seam the graph maps over. `outside` and `inside` therefore count cells,
    not rows: a three-column decomposition runs three of each.
    """
    calls = {
        "decompose": 0,
        "lenses": 0,
        "outside": 0,
        "inside": 0,
        "reflect": 0,
        "synthesize": 0,
    }

    async def fake_decompose(input, deps):
        calls["decompose"] += 1
        return a_decomposition()

    async def fake_choose_lenses(input, decomposition, sub_claim, deps):
        calls["lenses"] += 1
        return SubClaimLenses(lenses=[a_lens(f"{sub_claim.id}-broad")])

    async def fake_research_lens(input, sub_claim, lens, deps):
        # Every lens measures 0.22, so the anchor is 0.22 regardless of how the chain
        # rule combines them and the clean-pass arithmetic below is exact.
        calls["outside"] += 1
        return SubClaimBaseRates(lens=ref(lens.name, 0.22), disagreement="")

    async def fake_adjust_lens(input, sub_claim, lens, already_controlled_for, deps):
        # Only the first sub-question's lens moves; the others return evidence they
        # judged to be noise. Net +0.06 on a 0.22 anchor is the 0.28 `a_forecast`
        # states, so a clean pass really is clean and does not trip `check_derivation`.
        calls["inside"] += 1
        moves = (
            [adjustment("up", 0.10), adjustment("down", 0.04)]
            if sub_claim.id == "sc1"
            else [adjustment("neutral", 0.0, is_noise=True)]
        )
        return SubClaimAdjustments(
            lens_name=lens.name,
            adjustments=moves,
            steel_man="regulators could block it",
            what_would_change_my_mind="a second request",
        )

    async def fake_reflect(input, d, o, adjustments, steel_mans, deps):
        calls["reflect"] += 1
        return Reflection(
            steel_man="regulators could block it",
            what_would_change_my_mind="a second request",
            bias_checks=all_bias_checks(),
        )

    async def fake_synthesize(input, d, o, i, violations, deps):
        calls["synthesize"] += 1
        calls.setdefault("violations_seen", []).append(list(violations))
        return a_forecast()

    monkeypatch.setattr(fg, "run_decompose", fake_decompose)
    monkeypatch.setattr(fg, "run_choose_lenses", fake_choose_lenses)
    monkeypatch.setattr(fg, "run_research_lens", fake_research_lens)
    monkeypatch.setattr(fg, "run_adjust_lens", fake_adjust_lens)
    monkeypatch.setattr(fg, "run_reflect", fake_reflect)
    monkeypatch.setattr(fg, "run_synthesize", fake_synthesize)
    return calls


async def visited_steps(state: ForecastState, deps: ForecastDeps) -> list[str]:
    """Run the graph and record the stage each step announced, in order.

    The beta graph runs tasks concurrently, so there is no single "current node" to read
    the way the old sequential walk did. The stages the steps emit are the real
    observable — and they are what the UI orders on too, so testing them tests the thing
    that actually has to stay correct.
    """
    seen: list[str] = []

    def record(type: str, payload: dict, sub_claim=None) -> None:
        if type == "stage":
            seen.append(payload["stage"])

    deps.emit = record
    await fg.forecast_graph.run(state=state, deps=deps)
    return seen


# ---------- sub-claim identity ----------


async def test_run_decompose_stamps_stable_ids(monkeypatch):
    """Ids come from `run_decompose`, not the model.

    Reference classes and adjustments point back at these, so they have to be unique and
    complete — a model asked for its own keys eventually hands back a duplicate.
    """
    from superforecaster.agents import decompose as dc

    async def fake_agent_run(*args, **kwargs):
        class R:
            output = a_decomposition()

        return R()

    monkeypatch.setattr(dc, "run_agent", fake_agent_run)
    d = await dc.run_decompose(forecast_input(), ForecastDeps())
    assert [s.id for s in d.sub_claims] == ["sc1", "sc2", "sc3"]


# ---------- node order ----------


async def test_visits_nodes_in_methodology_order(stub_agents):
    """Principle 4 is this assertion. The base rate is found before it is adjusted."""
    state = ForecastState(input=forecast_input())
    seen = await visited_steps(state, ForecastDeps())

    assert seen == [
        "decompose", "lenses", "outside", "inside", "reflect", "synth", "critique"
    ]


async def test_outside_view_runs_before_inside_view(stub_agents):
    """The same guarantee stated as an ordering, in case the list above is edited."""
    state = ForecastState(input=forecast_input())
    seen = await visited_steps(state, ForecastDeps())
    assert seen.index("outside") < seen.index("inside")


async def test_each_research_step_runs_once_on_a_clean_pass(stub_agents):
    state = ForecastState(input=forecast_input())
    await visited_steps(state, ForecastDeps())

    assert stub_agents["decompose"] == 1
    # One cell per column, not one call per row — the decomposition has three.
    assert stub_agents["outside"] == 3
    assert stub_agents["inside"] == 3
    assert stub_agents["reflect"] == 1
    assert stub_agents["synthesize"] == 1


# ---------- the retry loop ----------


async def test_clean_forecast_ends_without_retrying(stub_agents):
    state = ForecastState(input=forecast_input())
    seen = await visited_steps(state, ForecastDeps())

    assert seen.count("synth") == 1
    assert state.violations == []


async def test_violation_routes_back_to_synthesize_exactly_once(
    monkeypatch, stub_agents
):
    """A blocking violation buys one more attempt — not an unbounded loop."""
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(
                principle=6, name="derivation", detail="injected", blocking=True
            )
        ],
    )
    state = ForecastState(input=forecast_input())
    seen = await visited_steps(state, ForecastDeps())

    assert seen.count("synth") == 2
    assert seen.count("critique") == 2
    assert stub_agents["synthesize"] == 2


async def test_retry_does_not_redo_the_research(monkeypatch, stub_agents):
    """Only synthesis is retried — re-running searches would be waste."""
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(
                principle=6, name="derivation", detail="injected", blocking=True
            )
        ],
    )
    await visited_steps(ForecastState(input=forecast_input()), ForecastDeps())

    assert stub_agents["outside"] == 3
    assert stub_agents["inside"] == 3
    assert stub_agents["reflect"] == 1


async def test_retry_tells_the_agent_what_failed(monkeypatch, stub_agents):
    """The loop is a correction, not a re-roll — attempt 2 sees the violation."""
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(
                principle=6, name="derivation", detail="injected", blocking=True
            )
        ],
    )
    await visited_steps(ForecastState(input=forecast_input()), ForecastDeps())

    first, second = stub_agents["violations_seen"]
    assert first == []
    assert [v.name for v in second] == ["derivation"]


async def test_ends_after_two_attempts_even_if_still_failing(monkeypatch, stub_agents):
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(
                principle=6, name="derivation", detail="injected", blocking=True
            )
        ],
    )
    state = ForecastState(input=forecast_input())
    seen = await visited_steps(state, ForecastDeps())

    assert seen.count("synth") == fg.MAX_SYNTHESIS_ATTEMPTS
    assert seen[-1] == "critique"


async def test_non_blocking_violation_does_not_retry(monkeypatch, stub_agents):
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(principle=8, name="advisory", detail="fyi", blocking=False)
        ],
    )
    state = ForecastState(input=forecast_input())
    seen = await visited_steps(state, ForecastDeps())
    assert seen.count("synth") == 1


# ---------- entry point ----------


async def test_run_forecast_graph_returns_surviving_violations(
    monkeypatch, stub_agents
):
    """A forecast that never satisfied its own methodology must not look clean."""
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(
                principle=6, name="derivation", detail="injected", blocking=True
            )
        ],
    )
    forecast, violations = await fg.run_forecast_graph(forecast_input())

    assert forecast.probability == 0.28
    assert [v.name for v in violations] == ["derivation"]


async def test_run_forecast_graph_restamps_question_metadata(stub_agents, monkeypatch):
    """The model does not get to restate the question it was asked."""

    async def drifting_synthesize(input, d, o, i, violations, deps):
        return a_forecast().model_copy(update={"question": "something else entirely"})

    monkeypatch.setattr(fg, "run_synthesize", drifting_synthesize)
    forecast, _ = await fg.run_forecast_graph(forecast_input())

    assert forecast.question == "Will A acquire B?"


async def test_deps_carry_both_clamps_into_the_run(stub_agents, monkeypatch):
    """as_of and model must reach the agents, or a backtest is silently contaminated."""
    seen: dict = {}

    async def capture(input, deps):
        seen["as_of"] = deps.as_of
        seen["model"] = deps.model
        return a_decomposition()

    monkeypatch.setattr(fg, "run_decompose", capture)
    as_of = datetime(2022, 2, 1, tzinfo=timezone.utc)
    await fg.run_forecast_graph(forecast_input(), as_of=as_of, model="anthropic:old")

    assert seen["as_of"] == as_of
    assert seen["model"] == "anthropic:old"


# ---------- diagram ----------


def test_mermaid_shows_the_retry_edge():
    """The docs render from this, so the loop has to be visible in it."""
    code = fg.forecast_mermaid()
    # Each research row forks through a decision, so an empty row can bypass the
    # map rather than stalling in it.
    assert "decompose --> decision" in code
    assert "--> choose_lenses_cell" in code
    assert "--> research_lens_cell" in code
    assert "--> adjust_lens_cell" in code
    # The retry cycle: critique routes through a decision back to synthesize.
    assert "critique --> decision" in code
    assert "--> synthesize" in code
