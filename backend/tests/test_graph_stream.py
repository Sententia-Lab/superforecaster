"""Tests for the streaming seam on the forecast graph.

Streaming used to be a `GraphHooks` protocol the caller passed in, with the graph driven
node-by-node to fire it. It is now ordinary events: each step announces its own stage
through `deps.emit`, the same sink the agents already write tool calls and token deltas
to. There is one mechanism instead of two, and a run that nobody is watching simply has
`emit=None`.

What has to stay true: stages arrive in methodology order, a stage opens before it
closes, the retry shows up as a second `synth` with a higher attempt number, and a
hookless run is byte-identical to what the CLI and evals have always got.
"""

from __future__ import annotations

from superforecaster.graphs import forecast as fg
from tests.test_graph_forecast import (  # noqa: F401 — stub_agents is a fixture
    a_decomposition,
    a_forecast,
    forecast_input,
    stub_agents,
)


class Recorder:
    """Collects everything a run emits, and the stage timeline out of it."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str | None]] = []

    def __call__(self, type: str, payload: dict, sub_claim: str | None = None) -> None:
        self.events.append((type, payload, sub_claim))

    @property
    def stages(self) -> list[str]:
        return [p["stage"] for t, p, _ in self.events if t == "stage"]

    @property
    def attempts(self) -> list[tuple[str, int]]:
        return [(p["stage"], p["attempt"]) for t, p, _ in self.events if t == "stage"]

    @property
    def timeline(self) -> list[str]:
        return [
            f"{'start' if t == 'stage' else 'finish'}:{p['stage']}"
            for t, p, _ in self.events
            if t in ("stage", "stage_end")
        ]


async def test_a_run_nobody_watches_is_unchanged(stub_agents):  # noqa: F811
    """The regression guard. Without `emit` nothing about the graph changed."""
    forecast, violations = await fg.run_forecast_graph(forecast_input())

    assert stub_agents == {
        "decompose": 1,
        # One lens chosen per researchable sub-question, then one cell per lens. The
        # decomposition has three researchable sub-questions and the stub names one lens
        # each, so the two research rows run three cells apiece.
        "lenses": 3,
        "outside": 3,
        "inside": 3,
        "reflect": 1,
        "synthesize": 1,
        "violations_seen": [[]],
    }
    assert forecast.probability == 0.28
    assert violations == []


async def test_emitting_does_not_change_the_result(stub_agents):  # noqa: F811
    plain, _ = await fg.run_forecast_graph(forecast_input())
    watched, _ = await fg.run_forecast_graph(forecast_input(), emit=Recorder())

    assert plain.probability == watched.probability
    assert plain.question == watched.question


async def test_all_six_stages_arrive_in_methodology_order(stub_agents):  # noqa: F811
    rec = Recorder()
    await fg.run_forecast_graph(forecast_input(), emit=rec)

    assert rec.stages == [
        "decompose",
        "lenses",
        "outside",
        "inside",
        "reflect",
        "synth",
        "critique",
    ]


async def test_a_stage_opens_before_it_closes(stub_agents):  # noqa: F811
    """The ordering a UI depends on: a stage must render as busy while it works, which
    only holds if the stage event precedes the agent rather than following it."""
    rec = Recorder()
    await fg.run_forecast_graph(forecast_input(), emit=rec)

    assert rec.timeline[:4] == [
        "start:decompose",
        "finish:decompose",
        "start:lenses",
        "finish:lenses",
    ]


async def test_every_stage_is_a_known_stage(stub_agents):  # noqa: F811
    """A step that announced a stage `STAGE_ORDER` does not know about would sort to the
    front of the run header, which is a bad place to find out."""
    rec = Recorder()
    await fg.run_forecast_graph(forecast_input(), emit=rec)

    assert set(rec.stages) <= set(fg.STAGE_ORDER)
    assert rec.stages == [s for s in fg.STAGE_ORDER if s in rec.stages]


async def test_the_retry_shows_up_as_a_second_synth(monkeypatch, stub_agents):  # noqa: F811
    """The attempt number is what distinguishes them — the UI needs no special
    knowledge of the retry to render it."""

    async def wandering_synthesize(input, d, o, i, violations, deps):
        stub_agents["synthesize"] += 1
        # 0.95 is nowhere near the 0.28 implied, so check_derivation blocks it.
        return a_forecast(0.95 if stub_agents["synthesize"] == 1 else 0.28)

    monkeypatch.setattr(fg, "run_synthesize", wandering_synthesize)

    rec = Recorder()
    await fg.run_forecast_graph(forecast_input(), emit=rec)

    assert rec.stages == [
        "decompose",
        "lenses",
        "outside",
        "inside",
        "reflect",
        "synth",
        "critique",
        "synth",
        "critique",
    ]
    retried = [(s, n) for s, n in rec.attempts if s in ("synth", "critique")]
    assert retried == [("synth", 1), ("critique", 1), ("synth", 2), ("critique", 2)]


async def test_emit_reaches_the_agents_through_deps(monkeypatch, stub_agents):  # noqa: F811
    """`emit` rides on ForecastDeps rather than a parameter, which is what lets the
    agents' own event stream handler reach it without any agent knowing about it."""
    rec = Recorder()

    async def spying_decompose(input, deps):
        assert deps.emit is not None
        deps.emit("thought", {"delta": "hello"}, None)
        return a_decomposition()

    monkeypatch.setattr(fg, "run_decompose", spying_decompose)
    await fg.run_forecast_graph(forecast_input(), emit=rec)

    assert ("thought", {"delta": "hello"}, None) in rec.events


async def test_no_emit_means_deps_carries_none(monkeypatch, stub_agents):  # noqa: F811
    """Production, CLI, cron, and evals must all be byte-identical to before."""
    captured = {}

    async def capture(input, deps):
        captured["emit"] = deps.emit
        return a_decomposition()

    monkeypatch.setattr(fg, "run_decompose", capture)
    await fg.run_forecast_graph(forecast_input())

    assert captured["emit"] is None


async def test_the_stage_result_is_the_model_the_agent_returned(stub_agents):  # noqa: F811
    """The backend does no reshaping. What lands on the wire for a stage is the typed
    object its agent produced, dumped — so the UI and the methodology cannot drift."""
    rec = Recorder()
    await fg.run_forecast_graph(forecast_input(), emit=rec)

    decompose = next(p for t, p, _ in rec.events if t == "decompose")
    assert [s["id"] for s in decompose["sub_claims"]] == ["sc1", "sc2", "sc3"]
    assert decompose["chain_note"] == "multiply"

    outside = next(p for t, p, _ in rec.events if t == "outside")
    assert "lenses" in outside and "aggregate_base_rate" in outside
