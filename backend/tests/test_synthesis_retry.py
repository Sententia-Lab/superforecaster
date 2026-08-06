"""The synthesis retry only fires for violations synthesis can actually fix.

The synthesize agent controls the final probability, reasoning, justification, and
carried decomposition ids. A blocking violation that indicts evidence from an earlier
stage — a lens whose counted hits disagree with its analogs, say — cannot be repaired
by rewriting the forecast, and retrying against it burns a full agent call per run.
"""

from __future__ import annotations

import pytest

from superforecaster import checks, stages
from superforecaster.models import CheckViolation, ForecastInput

from .gated_factories import (
    base_rate_payload,
    decomposition,
    forecast,
    future,
    inside_payload,
    reflection,
)


def _input() -> ForecastInput:
    return ForecastInput(
        question="Will it happen?",
        resolution_criteria="It observably happens.",
        resolution_date=future(),
        category="test",
    )


@pytest.fixture
def synthesis_seams(monkeypatch):
    """Stub the two agents; let the caller control what the checks return."""
    calls = {"synthesize": 0}

    async def fake_reflect(input, decomp, outside, adjustments, steel_mans, deps):
        return reflection()

    async def fake_synthesize(input, decomp, outside, inside, violations, deps):
        calls["synthesize"] += 1
        return forecast()

    monkeypatch.setattr(stages, "run_reflect", fake_reflect)
    monkeypatch.setattr(stages, "run_synthesize", fake_synthesize)
    return calls


def _violation(name: str, principle: int = 4, blocking: bool = True) -> CheckViolation:
    return CheckViolation(
        principle=principle, name=name, detail="d", blocking=blocking
    )


async def _run_stage():
    return await stages.run_synthesis_stage(
        _input(),
        decomposition(),
        [(decomposition().sub_claims[0], base_rate_payload())],
        [(decomposition().sub_claims[0], inside_payload())],
        stages.ForecastDeps(),
    )


@pytest.mark.asyncio
async def test_unfixable_blocking_violation_does_not_retry(
    synthesis_seams, monkeypatch
):
    """`base_rates` audits the research cells' evidence — synthesis cannot fix it."""
    monkeypatch.setattr(
        checks,
        "run_forecast_checks",
        lambda *a, **k: [_violation("base_rates")],
    )
    payload = await _run_stage()
    assert synthesis_seams["synthesize"] == 1
    assert payload.attempts == 1
    assert payload.violations[0].name == "base_rates"


@pytest.mark.asyncio
async def test_fixable_blocking_violation_retries_once(synthesis_seams, monkeypatch):
    monkeypatch.setattr(
        checks,
        "run_forecast_checks",
        lambda *a, **k: [_violation("derivation", principle=6)],
    )
    payload = await _run_stage()
    assert synthesis_seams["synthesize"] == 2
    assert payload.attempts == 2


@pytest.mark.asyncio
async def test_clean_forecast_runs_once(synthesis_seams, monkeypatch):
    monkeypatch.setattr(checks, "run_forecast_checks", lambda *a, **k: [])
    payload = await _run_stage()
    assert synthesis_seams["synthesize"] == 1
    assert payload.attempts == 1


@pytest.mark.asyncio
async def test_advisory_violations_never_trigger_a_retry(synthesis_seams, monkeypatch):
    monkeypatch.setattr(
        checks,
        "run_forecast_checks",
        lambda *a, **k: [
            _violation("calibration_hygiene", principle=16, blocking=False)
        ],
    )
    payload = await _run_stage()
    assert synthesis_seams["synthesize"] == 1


def test_fixable_set_names_real_checks():
    """Every name in the fixable set must exist in the checks table, so a renamed
    check cannot silently turn the retry off."""
    known = {entry[0] for entry in checks.FORECAST_CHECKS}
    assert stages.SYNTHESIS_FIXABLE <= known
