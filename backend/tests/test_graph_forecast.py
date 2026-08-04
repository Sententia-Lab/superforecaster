"""Tests for the forecast graph's wiring.

These test routing, not agent quality. The agents are stubbed out — what is being
verified is that the graph visits nodes in the right order and takes the retry edge
the right number of times.

Node order is the point of the whole exercise: principle 4 ("outside view first") is
enforced by `FindBaseRates` preceding `AdjustInsideView` in the graph rather than by
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
    OutsideView,
    ResearchSummary,
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
    return Decomposition(sub_claims=[sub(), sub(), sub()], chain_note="multiply")


def an_outside_view() -> OutsideView:
    return OutsideView(
        reference_classes=[ref("a", 0.20), ref("b", 0.24)],
        aggregate_base_rate=0.22,
        disagreement="",
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
        confidence="medium",
        decompositions=[sub(), sub(), sub()],
        research=ResearchSummary(),
        reasoning="base rate then adjustments",
    )


@pytest.fixture
def stub_agents(monkeypatch):
    """Replace every agent with a canned, methodology-clean result.

    Returns a dict recording how many times each step ran, so the retry loop is
    observable.
    """
    calls = {"decompose": 0, "outside": 0, "inside": 0, "synthesize": 0}

    async def fake_decompose(input, deps):
        calls["decompose"] += 1
        return a_decomposition()

    async def fake_outside(input, decomposition, deps):
        calls["outside"] += 1
        return an_outside_view()

    async def fake_inside(input, outside, deps):
        calls["inside"] += 1
        return an_inside_view()

    async def fake_synthesize(input, d, o, i, violations, deps):
        calls["synthesize"] += 1
        calls.setdefault("violations_seen", []).append(list(violations))
        return a_forecast()

    monkeypatch.setattr(fg, "run_decompose", fake_decompose)
    monkeypatch.setattr(fg, "run_outside_view", fake_outside)
    monkeypatch.setattr(fg, "run_inside_view", fake_inside)
    monkeypatch.setattr(fg, "run_synthesize", fake_synthesize)
    return calls


async def visited_nodes(state: ForecastState, deps: ForecastDeps) -> list[str]:
    """Walk the graph and record the class name of every node it enters."""
    seen: list[str] = []
    async with fg.forecast_graph.iter(fg.Decompose(), state=state, deps=deps) as run:
        async for node in run:
            seen.append(type(node).__name__)
    return seen


# ---------- node order ----------


async def test_visits_nodes_in_methodology_order(stub_agents):
    """Principle 4 is this assertion. The base rate is found before it is adjusted."""
    state = ForecastState(input=forecast_input())
    seen = await visited_nodes(state, ForecastDeps())

    assert seen[:5] == [
        "Decompose",
        "FindBaseRates",
        "AdjustInsideView",
        "Synthesize",
        "Critique",
    ]


async def test_outside_view_runs_before_inside_view(stub_agents):
    """The same guarantee stated as an ordering, in case the list above is edited."""
    state = ForecastState(input=forecast_input())
    seen = await visited_nodes(state, ForecastDeps())
    assert seen.index("FindBaseRates") < seen.index("AdjustInsideView")


async def test_each_research_step_runs_once_on_a_clean_pass(stub_agents):
    state = ForecastState(input=forecast_input())
    await visited_nodes(state, ForecastDeps())

    assert stub_agents["decompose"] == 1
    assert stub_agents["outside"] == 1
    assert stub_agents["inside"] == 1
    assert stub_agents["synthesize"] == 1


# ---------- the retry loop ----------


async def test_clean_forecast_ends_without_retrying(stub_agents):
    state = ForecastState(input=forecast_input())
    seen = await visited_nodes(state, ForecastDeps())

    assert seen.count("Synthesize") == 1
    assert state.violations == []


async def test_violation_routes_back_to_synthesize_exactly_once(monkeypatch, stub_agents):
    """A blocking violation buys one more attempt — not an unbounded loop."""
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(principle=6, name="derivation", detail="injected", blocking=True)
        ],
    )
    state = ForecastState(input=forecast_input())
    seen = await visited_nodes(state, ForecastDeps())

    assert seen.count("Synthesize") == 2
    assert seen.count("Critique") == 2
    assert stub_agents["synthesize"] == 2


async def test_retry_does_not_redo_the_research(monkeypatch, stub_agents):
    """Only synthesis is retried — re-running searches would be waste."""
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(principle=6, name="derivation", detail="injected", blocking=True)
        ],
    )
    await visited_nodes(ForecastState(input=forecast_input()), ForecastDeps())

    assert stub_agents["outside"] == 1
    assert stub_agents["inside"] == 1


async def test_retry_tells_the_agent_what_failed(monkeypatch, stub_agents):
    """The loop is a correction, not a re-roll — attempt 2 sees the violation."""
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(principle=6, name="derivation", detail="injected", blocking=True)
        ],
    )
    await visited_nodes(ForecastState(input=forecast_input()), ForecastDeps())

    first, second = stub_agents["violations_seen"]
    assert first == []
    assert [v.name for v in second] == ["derivation"]


async def test_ends_after_two_attempts_even_if_still_failing(monkeypatch, stub_agents):
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(principle=6, name="derivation", detail="injected", blocking=True)
        ],
    )
    state = ForecastState(input=forecast_input())
    seen = await visited_nodes(state, ForecastDeps())

    assert seen.count("Synthesize") == fg.MAX_SYNTHESIS_ATTEMPTS
    assert "End" in seen[-1]


async def test_non_blocking_violation_does_not_retry(monkeypatch, stub_agents):
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(principle=8, name="advisory", detail="fyi", blocking=False)
        ],
    )
    state = ForecastState(input=forecast_input())
    seen = await visited_nodes(state, ForecastDeps())
    assert seen.count("Synthesize") == 1


# ---------- entry point ----------


async def test_run_forecast_graph_returns_surviving_violations(monkeypatch, stub_agents):
    """A forecast that never satisfied its own methodology must not look clean."""
    monkeypatch.setattr(
        fg.checks,
        "run_forecast_checks",
        lambda *a, **k: [
            CheckViolation(principle=6, name="derivation", detail="injected", blocking=True)
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
    assert "Decompose --> FindBaseRates" in code
    assert "FindBaseRates --> AdjustInsideView" in code
    assert "Critique --> Synthesize" in code
