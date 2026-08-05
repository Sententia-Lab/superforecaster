"""Tests for the streaming seam on the forecast graph.

The regression that matters: adding `hooks` must not have changed what the graph does
when nobody passes them. `run_forecast_graph` drives `.iter()` with hooks and
`.run()` without, and those two paths have to agree on every node visited.
"""

from __future__ import annotations

from dataclasses import dataclass, field


from superforecaster.graphs import forecast as fg
from superforecaster.graphs.state import ForecastState
from tests.test_graph_forecast import (  # noqa: F401 — stub_agents is a fixture
    a_decomposition,
    a_forecast,
    forecast_input,
    stub_agents,
)


@dataclass
class RecordingHooks:
    """A `GraphHooks` that writes down what it saw."""

    started: list[tuple[str, int]] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    def stage_started(self, stage: str, attempt: int, state: ForecastState) -> None:
        self.started.append((stage, attempt))
        self.order.append(f"start:{stage}")

    def stage_finished(self, stage: str, state: ForecastState) -> None:
        self.finished.append(stage)
        self.order.append(f"finish:{stage}")


async def test_hookless_run_visits_the_same_nodes_as_before(stub_agents):  # noqa: F811
    """The regression guard. Without hooks nothing about the graph changed."""
    forecast, violations = await fg.run_forecast_graph(forecast_input())

    assert stub_agents == {
        "decompose": 1,
        "outside": 1,
        "inside": 1,
        "synthesize": 1,
        "violations_seen": [[]],
    }
    assert forecast.probability == 0.28
    assert violations == []


async def test_hooks_and_no_hooks_agree_on_the_result(stub_agents):  # noqa: F811
    plain, _ = await fg.run_forecast_graph(forecast_input())
    hooked, _ = await fg.run_forecast_graph(forecast_input(), hooks=RecordingHooks())

    assert plain.probability == hooked.probability
    assert plain.question == hooked.question


async def test_hooks_see_all_five_stages_in_methodology_order(stub_agents):  # noqa: F811
    hooks = RecordingHooks()
    await fg.run_forecast_graph(forecast_input(), hooks=hooks)

    assert [s for s, _ in hooks.started] == [
        "decompose",
        "outside",
        "inside",
        "synth",
        "critique",
    ]
    assert hooks.finished == [s for s, _ in hooks.started]


async def test_a_stage_starts_before_it_finishes(stub_agents):  # noqa: F811
    """The ordering a UI depends on: a stage must be able to render as busy while it
    works, which only holds if `stage_started` fires before the node's agent runs."""
    hooks = RecordingHooks()
    await fg.run_forecast_graph(forecast_input(), hooks=hooks)

    assert hooks.order[:4] == [
        "start:decompose",
        "finish:decompose",
        "start:outside",
        "finish:outside",
    ]


async def test_the_retry_edge_shows_up_as_a_second_synthesize(monkeypatch, stub_agents):  # noqa: F811
    """Synthesize is simply yielded twice, and the attempt number distinguishes them —
    the UI needs no special knowledge of the retry to render it."""

    async def wandering_synthesize(input, d, o, i, violations, deps):
        stub_agents["synthesize"] += 1
        # 0.95 is nowhere near the 0.28 implied, so check_derivation blocks it.
        return a_forecast(0.95 if stub_agents["synthesize"] == 1 else 0.28)

    monkeypatch.setattr(fg, "run_synthesize", wandering_synthesize)

    hooks = RecordingHooks()
    await fg.run_forecast_graph(forecast_input(), hooks=hooks)

    assert [s for s, _ in hooks.started] == [
        "decompose",
        "outside",
        "inside",
        "synth",
        "critique",
        "synth",
        "critique",
    ]
    attempts = [(s, n) for s, n in hooks.started if s in ("synth", "critique")]
    assert attempts == [("synth", 1), ("critique", 1), ("synth", 2), ("critique", 2)]


async def test_emit_reaches_the_agents_through_deps(monkeypatch, stub_agents):  # noqa: F811
    """`emit` rides on ForecastDeps rather than a parameter, which is what lets the
    agents' own event stream handler reach it without any agent knowing about it."""
    emitted: list[tuple[str, dict, str | None]] = []

    async def spying_decompose(input, deps):
        assert deps.emit is not None
        deps.emit("thought", {"delta": "hello"}, None)
        return a_decomposition()

    monkeypatch.setattr(fg, "run_decompose", spying_decompose)
    await fg.run_forecast_graph(
        forecast_input(), emit=lambda t, p, sc=None: emitted.append((t, p, sc))
    )

    assert emitted == [("thought", {"delta": "hello"}, None)]


async def test_no_emit_means_deps_carries_none(monkeypatch, stub_agents):  # noqa: F811
    """Production, CLI, cron, and evals must all be byte-identical to before."""
    captured = {}

    async def capture(input, deps):
        captured["emit"] = deps.emit
        return a_decomposition()

    monkeypatch.setattr(fg, "run_decompose", capture)
    await fg.run_forecast_graph(forecast_input())

    assert captured["emit"] is None


def test_every_node_class_maps_to_a_stage_key():
    """A node added without a STAGE_KEYS entry would KeyError mid-run, which is a bad
    place to find out."""
    node_names = {n.__name__ for n in fg.forecast_graph.get_nodes()}
    assert node_names == set(fg.STAGE_KEYS)
