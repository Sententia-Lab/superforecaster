"""`stages.run_all` — the CLI/eval driver — produces a Forecast with no gates."""

from __future__ import annotations

import pytest

from superforecaster import stages
from superforecaster.models import Forecast, ForecastInput

from .gated_factories import (
    base_rate_payload,
    chosen_lenses,
    decomposition,
    future,
    inside_payload,
    synthesis_payload,
)


@pytest.mark.asyncio
async def test_run_all_drives_every_stage_and_returns_the_forecast(monkeypatch):
    seen: list[str] = []

    async def fake_decompose(input, deps):
        seen.append("decompose")
        return decomposition()

    async def fake_lenses(input, decomp, sub_claim, deps):
        seen.append(f"lenses:{sub_claim.id}")
        return chosen_lenses("lens-a")

    async def fake_base_rate(input, sub_claim, lens, deps):
        seen.append(f"base:{sub_claim.id}:{lens.name}")
        return base_rate_payload(lens.name, sub_claim.id)

    async def fake_inside(input, sub_claim, payload, deps):
        seen.append(f"inside:{sub_claim.id}:{payload.lens.name}")
        return inside_payload(payload.lens.name, sub_claim.id)

    async def fake_synthesis(input, decomp, base_cells, inside_cells, deps):
        seen.append("synthesis")
        assert len(base_cells) == 2  # sc1 and sc3 × one lens each
        assert len(inside_cells) == 2
        return synthesis_payload()

    monkeypatch.setattr(stages, "run_decompose_stage", fake_decompose)
    monkeypatch.setattr(stages, "run_lenses_stage", fake_lenses)
    monkeypatch.setattr(stages, "run_base_rate_step", fake_base_rate)
    monkeypatch.setattr(stages, "run_inside_step", fake_inside)
    monkeypatch.setattr(stages, "run_synthesis_stage", fake_synthesis)

    forecast, violations = await stages.run_all(
        ForecastInput(
            question="Will it happen?",
            resolution_criteria="It observably happens.",
            resolution_date=future(),
            category="test",
        )
    )

    assert isinstance(forecast, Forecast)
    assert violations and violations[0].name == "derivation"
    assert seen[0] == "decompose"
    assert seen[-1] == "synthesis"
    # Structural ordering: every lens choice precedes every measurement, every
    # measurement precedes every adjustment.
    assert max(i for i, s in enumerate(seen) if s.startswith("lenses:")) < min(
        i for i, s in enumerate(seen) if s.startswith("base:")
    )
    assert max(i for i, s in enumerate(seen) if s.startswith("base:")) < min(
        i for i, s in enumerate(seen) if s.startswith("inside:")
    )


@pytest.mark.asyncio
async def test_run_all_survives_a_failing_cell(monkeypatch):
    async def fake_decompose(input, deps):
        return decomposition()

    async def fake_lenses(input, decomp, sub_claim, deps):
        return chosen_lenses("lens-a")

    async def flaky_base_rate(input, sub_claim, lens, deps):
        if sub_claim.id == "sc1":
            raise RuntimeError("cell died")
        return base_rate_payload(lens.name, sub_claim.id)

    async def fake_inside(input, sub_claim, payload, deps):
        return inside_payload(payload.lens.name, sub_claim.id)

    async def fake_synthesis(input, decomp, base_cells, inside_cells, deps):
        # sc1's cell died; the row degrades to what the other populations found.
        assert [c.id for c, _ in base_cells] == ["sc3"]
        return synthesis_payload()

    monkeypatch.setattr(stages, "run_decompose_stage", fake_decompose)
    monkeypatch.setattr(stages, "run_lenses_stage", fake_lenses)
    monkeypatch.setattr(stages, "run_base_rate_step", flaky_base_rate)
    monkeypatch.setattr(stages, "run_inside_step", fake_inside)
    monkeypatch.setattr(stages, "run_synthesis_stage", fake_synthesis)

    forecast, _violations = await stages.run_all(
        ForecastInput(
            question="Will it happen?",
            resolution_criteria="It observably happens.",
            resolution_date=future(),
            category="test",
        )
    )
    assert isinstance(forecast, Forecast)
