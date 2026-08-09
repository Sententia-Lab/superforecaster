"""Integration tests for the /forecasts API.

The forecast/refresh agents are mocked — these tests verify the endpoint
contracts, auth, and that the DB is updated correctly. The agents
themselves are tested separately (or by manual fixture-driven runs).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from superforecaster.models import (
    UpdateOutcome,
    Forecast,
    HistoricalAnalog,
    RefreshActionResponse,
    ResearchSummary,
    SubPrediction,
)

ADMIN_KEY = "test-admin-key"


@pytest.fixture(autouse=True)
def _set_admin_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)


@asynccontextmanager
async def _noop_lifespan(app):
    from superforecaster import db

    db.init_db()
    yield


@pytest.fixture
def client() -> TestClient:
    app.router.lifespan_context = _noop_lifespan
    return TestClient(app)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def _future_iso(days: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _mock_forecast() -> Forecast:
    return Forecast(
        question="Will X happen?",
        resolution_criteria="X is observable.",
        resolution_date=datetime.now(timezone.utc) + timedelta(days=60),
        category="test",
        probability=0.55,
        confidence="medium",
        decompositions=[
            SubPrediction(
                question=f"Sub {i}?",
                probability=0.5,
                rationale="r",
                confidence="medium",
            )
            for i in range(3)
        ],
        research=ResearchSummary(
            historical_analogs=[
                HistoricalAnalog(description="A", outcome=1.0, relevance="r"),
                HistoricalAnalog(description="B", outcome=0.0, relevance="r"),
                HistoricalAnalog(description="C", outcome=1.0, relevance="r"),
            ],
            empirical_base_rate=2 / 3,
            base_rate_note="ok",
        ),
        reasoning="Mock reasoning.",
    )


def test_create_forecast_requires_admin(client):
    resp = client.post(
        "/forecasts",
        json={
            "question": "Q",
            "resolution_criteria": "X",
            "resolution_source": "src",
            "resolution_date": _future_iso(),
            "category": "test",
        },
    )
    assert resp.status_code == 403


def test_create_forecast(client, admin_headers):
    async def mock_run_forecast(input):
        # run_all returns (forecast, surviving violations)
        return (
            _mock_forecast().model_copy(
                update={
                    "question": input.question,
                    "resolution_criteria": input.resolution_criteria,
                    "resolution_date": input.resolution_date,
                    "category": input.category,
                }
            ),
            [],
        )

    with patch("api.forecasts.run_all", side_effect=mock_run_forecast):
        resp = client.post(
            "/forecasts",
            json={
                "question": "Will X happen?",
                "resolution_criteria": "X is observable.",
                "resolution_source": "test",
                "resolution_date": _future_iso(),
                "category": "test",
            },
            headers=admin_headers,
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["question"] == "Will X happen?"
    assert data["outcome"] is None
    assert len(data["updates"]) == 1


def test_list_and_get_forecast(client, admin_headers):
    async def mock_run_forecast(input):
        # run_all returns (forecast, surviving violations)
        return (
            _mock_forecast().model_copy(
                update={
                    "question": input.question,
                    "resolution_criteria": input.resolution_criteria,
                    "resolution_date": input.resolution_date,
                    "category": input.category,
                }
            ),
            [],
        )

    with patch("api.forecasts.run_all", side_effect=mock_run_forecast):
        create = client.post(
            "/forecasts",
            json={
                "question": "Q",
                "resolution_criteria": "X",
                "resolution_source": "src",
                "resolution_date": _future_iso(),
                "category": "test",
            },
            headers=admin_headers,
        )
    fid = create.json()["id"]

    listed = client.get("/forecasts").json()
    assert len(listed) == 1
    assert listed[0]["id"] == fid

    fetched = client.get(f"/forecasts/{fid}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == fid


def test_resolve_forecast(client, admin_headers):
    async def mock_run_forecast(input):
        # run_all returns (forecast, surviving violations)
        return (
            _mock_forecast().model_copy(
                update={
                    "question": input.question,
                    "resolution_criteria": input.resolution_criteria,
                    "resolution_date": input.resolution_date,
                    "category": input.category,
                }
            ),
            [],
        )

    with patch("api.forecasts.run_all", side_effect=mock_run_forecast):
        create = client.post(
            "/forecasts",
            json={
                "question": "Q",
                "resolution_criteria": "X",
                "resolution_source": "src",
                "resolution_date": _future_iso(),
                "category": "test",
            },
            headers=admin_headers,
        )
    fid = create.json()["id"]

    resolved = client.patch(
        f"/forecasts/{fid}/resolve",
        json={"outcome": 1.0},
        headers=admin_headers,
    )
    assert resolved.status_code == 200
    data = resolved.json()
    assert data["outcome"] == 1.0
    assert data["scored_probability"] is not None
    assert data["brier_score"] is not None


def test_resolve_with_null_marks_ambiguous(client, admin_headers):
    async def mock_run_forecast(input):
        # run_all returns (forecast, surviving violations)
        return (
            _mock_forecast().model_copy(
                update={
                    "question": input.question,
                    "resolution_criteria": input.resolution_criteria,
                    "resolution_date": input.resolution_date,
                    "category": input.category,
                }
            ),
            [],
        )

    with patch("api.forecasts.run_all", side_effect=mock_run_forecast):
        create = client.post(
            "/forecasts",
            json={
                "question": "Q",
                "resolution_criteria": "X",
                "resolution_source": "src",
                "resolution_date": _future_iso(),
                "category": "test",
            },
            headers=admin_headers,
        )
    fid = create.json()["id"]

    resolved = client.patch(
        f"/forecasts/{fid}/resolve",
        json={"outcome": None},
        headers=admin_headers,
    )
    assert resolved.status_code == 200
    data = resolved.json()
    assert data["is_ambiguous"] is True
    assert data["outcome"] is None


def test_manual_refresh_endpoint(client, admin_headers):
    async def mock_run_forecast(input):
        # run_all returns (forecast, surviving violations)
        return (
            _mock_forecast().model_copy(
                update={
                    "question": input.question,
                    "resolution_criteria": input.resolution_criteria,
                    "resolution_date": input.resolution_date,
                    "category": input.category,
                }
            ),
            [],
        )

    with patch("api.forecasts.run_all", side_effect=mock_run_forecast):
        create = client.post(
            "/forecasts",
            json={
                "question": "Q",
                "resolution_criteria": "X",
                "resolution_source": "src",
                "resolution_date": _future_iso(),
                "category": "test",
            },
            headers=admin_headers,
        )
    fid = create.json()["id"]

    async def mock_refresh(forecast_id):
        return UpdateOutcome(updated=False, reason="no change")

    with patch("api.forecasts.run_update_graph", side_effect=mock_refresh):
        resp = client.post(f"/forecasts/{fid}/refresh", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["updated"] is False


def test_calibration_returns_zero_with_no_resolved(client):
    resp = client.get("/calibration")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_resolved"] == 0
    assert data["aggregate_brier_score"] is None


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
