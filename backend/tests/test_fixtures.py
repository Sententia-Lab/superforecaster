"""Sanity tests that the bundled fixtures parse into valid records.

These don't call any agents — they just verify the JSON structure
matches what the CLI's `--fixture` paths expect.
"""

from __future__ import annotations

import json

import pytest

from app.cli import _record_from_fixture
from superforecaster.models import ForecastInput


@pytest.fixture
def forecast_question_data(fixtures_dir):
    return json.loads((fixtures_dir / "forecast_question.json").read_text())


@pytest.fixture
def existing_forecast_data(fixtures_dir):
    return json.loads((fixtures_dir / "existing_forecast.json").read_text())


def test_forecast_question_fixture_parses_into_forecast_input(forecast_question_data):
    """The forecast fixture has all fields needed to construct a ForecastInput."""
    from datetime import datetime

    inp = ForecastInput(
        question=forecast_question_data["question"],
        resolution_criteria=forecast_question_data["resolution_criteria"],
        resolution_date=datetime.fromisoformat(
            forecast_question_data["resolution_date"].replace("Z", "+00:00")
        ),
        category=forecast_question_data["category"],
    )
    assert inp.question
    assert inp.resolution_criteria
    assert inp.category


def test_existing_forecast_fixture_builds_a_record(existing_forecast_data):
    record = _record_from_fixture(existing_forecast_data)
    assert record.id == "fixture-forecast-001"
    assert len(record.updates) == 2
    assert record.updates[0].probability == 0.71
    assert record.updates[1].probability == 0.64
    assert record.research.empirical_base_rate is not None
    assert len(record.research.historical_analogs) >= 3
