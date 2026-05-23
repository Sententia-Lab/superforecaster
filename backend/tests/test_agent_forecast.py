"""Tests for two-phase forecast pipeline convergence."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded

from superforecaster.agent import run_forecast
from superforecaster.models import (
    Forecast,
    ForecastInput,
    ForecastResearchNotes,
    ResearchSummary,
    SubPrediction,
)


def _sample_input() -> ForecastInput:
    return ForecastInput(
        question="Will X happen?",
        resolution_criteria="Resolves YES if X happens by 2026-12-31.",
        resolution_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
        category="politics",
        max_iterations=3,
    )


def _sample_research() -> ForecastResearchNotes:
    sub = SubPrediction(
        question="Will prerequisite A occur?",
        probability=0.4,
        rationale="Base rate suggests moderate chance.",
        confidence="medium",
    )
    return ForecastResearchNotes(
        decompositions=[sub],
        research=ResearchSummary(
            empirical_base_rate=0.35,
            base_rate_note="Three analogs found.",
        ),
        analysis_notes="Evidence mixed; synthesis should weigh inside view.",
    )


def _sample_forecast(input: ForecastInput) -> Forecast:
    subs = [
        SubPrediction(
            question=f"Sub-question {i}?",
            probability=0.3 + i * 0.1,
            rationale="Test rationale.",
            confidence="medium",
        )
        for i in range(3)
    ]
    return Forecast(
        question=input.question,
        resolution_criteria=input.resolution_criteria,
        resolution_date=input.resolution_date,
        category=input.category,
        probability=0.42,
        confidence="medium",
        decompositions=subs,
        research=ResearchSummary(empirical_base_rate=0.35),
        reasoning="Base rate anchored; adjusted for inside view.",
    )


@pytest.mark.asyncio
async def test_run_forecast_research_then_synthesis():
    input = _sample_input()
    research = _sample_research()
    forecast = _sample_forecast(input)

    async def fake_run_agent(agent, prompt, **kwargs):
        run_name = kwargs.get("run_name", "")
        if run_name == "forecast research":
            return AsyncMock(output=research)
        if run_name == "forecast synthesis":
            return AsyncMock(output=forecast)
        raise AssertionError(f"unexpected run_name: {run_name}")

    with patch("superforecaster.agent.run_agent", side_effect=fake_run_agent):
        result = await run_forecast(input)

    assert result.probability == 0.42
    assert result.question == input.question


@pytest.mark.asyncio
async def test_run_forecast_synthesizes_when_research_hits_limit():
    input = _sample_input()
    forecast = _sample_forecast(input)
    calls: list[str] = []

    async def fake_run_agent(agent, prompt, **kwargs):
        run_name = kwargs.get("run_name", "")
        calls.append(run_name)
        if run_name == "forecast research":
            raise UsageLimitExceeded("tool_calls_limit")
        if run_name == "forecast synthesis":
            assert "budget" in prompt.lower() or "partial" in prompt.lower()
            return AsyncMock(output=forecast)
        raise AssertionError(f"unexpected run_name: {run_name}")

    with patch("superforecaster.agent.run_agent", side_effect=fake_run_agent):
        result = await run_forecast(input)

    assert calls == ["forecast research", "forecast synthesis"]
    assert result.probability == 0.42
