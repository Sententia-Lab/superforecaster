"""Integration tests for the /questions API.

These hit the FastAPI app via TestClient. The forecast/refresh/resolution
agents are NOT called in these tests — those endpoints are exercised
separately with mocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app


ADMIN_KEY = "test-admin-key"


@pytest.fixture(autouse=True)
def _set_admin_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)


@pytest.fixture
def client() -> TestClient:
    """A TestClient with no scheduler — disable lifespan to avoid APScheduler."""
    app.router.lifespan_context = _noop_lifespan
    return TestClient(app)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def caller_headers() -> dict[str, str]:
    """Headers that simulate a caller IP for IP-based identity."""
    return {"X-Forwarded-For": "1.2.3.4"}


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _noop_lifespan(app):
    from superforecaster import db

    db.init_db()
    yield


def _future_iso(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_create_question(client, caller_headers):
    resp = client.post(
        "/questions",
        json={
            "text": "Will X happen?",
            "resolution_criteria": "X is observable.",
            "proposed_resolution_date": _future_iso(),
        },
        headers=caller_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["text"] == "Will X happen?"
    assert data["status"] == "pending"
    assert data["net_score"] == 0


def test_create_question_rate_limited(client, caller_headers):
    payload = {
        "text": "Q1",
        "resolution_criteria": "X.",
        "proposed_resolution_date": _future_iso(),
    }
    resp1 = client.post("/questions", json=payload, headers=caller_headers)
    assert resp1.status_code == 201

    resp2 = client.post(
        "/questions",
        json={**payload, "text": "Q2"},
        headers=caller_headers,
    )
    assert resp2.status_code == 429


def test_vote_and_undo(client, caller_headers):
    voter_headers = {"X-Forwarded-For": "9.9.9.9"}

    create = client.post(
        "/questions",
        json={
            "text": "Q",
            "resolution_criteria": "X.",
            "proposed_resolution_date": _future_iso(),
        },
        headers=caller_headers,
    )
    qid = create.json()["id"]

    vote = client.post(
        f"/questions/{qid}/vote", json={"vote": 1}, headers=voter_headers
    )
    assert vote.status_code == 200
    assert vote.json()["net_score"] == 1
    assert vote.json()["user_vote"] == 1

    # Switch to downvote
    downvote = client.post(
        f"/questions/{qid}/vote", json={"vote": -1}, headers=voter_headers
    )
    assert downvote.json()["net_score"] == -1

    # Undo
    undo = client.delete(f"/questions/{qid}/vote", headers=voter_headers)
    assert undo.status_code == 200
    assert undo.json()["net_score"] == 0
    assert undo.json()["user_vote"] is None


def test_invalid_vote_value(client, caller_headers):
    create = client.post(
        "/questions",
        json={
            "text": "Q",
            "resolution_criteria": "X.",
            "proposed_resolution_date": _future_iso(),
        },
        headers=caller_headers,
    )
    qid = create.json()["id"]
    resp = client.post(
        f"/questions/{qid}/vote",
        json={"vote": 5},
        headers={"X-Forwarded-For": "9.9.9.9"},
    )
    assert resp.status_code == 400


def test_edit_question_only_by_submitter(client, caller_headers):
    create = client.post(
        "/questions",
        json={
            "text": "Q",
            "resolution_criteria": "X.",
            "proposed_resolution_date": _future_iso(),
        },
        headers=caller_headers,
    )
    qid = create.json()["id"]

    # Same IP can edit
    edit = client.put(
        f"/questions/{qid}",
        json={"text": "Q-edited"},
        headers=caller_headers,
    )
    assert edit.status_code == 200
    assert edit.json()["text"] == "Q-edited"

    # Different IP cannot
    resp = client.put(
        f"/questions/{qid}",
        json={"text": "hacked"},
        headers={"X-Forwarded-For": "9.9.9.9"},
    )
    assert resp.status_code == 403


def test_admin_can_edit_any_question(client, caller_headers, admin_headers):
    create = client.post(
        "/questions",
        json={
            "text": "Q",
            "resolution_criteria": "X.",
            "proposed_resolution_date": _future_iso(),
        },
        headers=caller_headers,
    )
    qid = create.json()["id"]

    resp = client.put(
        f"/questions/{qid}",
        json={"text": "admin-edit"},
        headers={"X-Forwarded-For": "9.9.9.9", **admin_headers},
    )
    assert resp.status_code == 200
    assert resp.json()["text"] == "admin-edit"


def test_delete_question(client, caller_headers):
    create = client.post(
        "/questions",
        json={
            "text": "Q",
            "resolution_criteria": "X.",
            "proposed_resolution_date": _future_iso(),
        },
        headers=caller_headers,
    )
    qid = create.json()["id"]

    resp = client.delete(f"/questions/{qid}", headers=caller_headers)
    assert resp.status_code == 204

    # After delete, GET returns 404
    fetch = client.get(f"/questions/{qid}")
    assert fetch.status_code == 404


def test_list_sorts_by_score(client):
    ips = ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
    qids = []
    for i, ip in enumerate(ips):
        resp = client.post(
            "/questions",
            json={
                "text": f"Q{i}",
                "resolution_criteria": "X.",
                "proposed_resolution_date": _future_iso(),
            },
            headers={"X-Forwarded-For": ip},
        )
        qids.append(resp.json()["id"])

    # Vote so q[1] gets the most, q[0] some, q[2] none
    voter1 = {"X-Forwarded-For": "9.9.9.1"}
    voter2 = {"X-Forwarded-For": "9.9.9.2"}
    client.post(f"/questions/{qids[1]}/vote", json={"vote": 1}, headers=voter1)
    client.post(f"/questions/{qids[1]}/vote", json={"vote": 1}, headers=voter2)
    client.post(f"/questions/{qids[0]}/vote", json={"vote": 1}, headers=voter1)

    resp = client.get("/questions?sort=score")
    assert resp.status_code == 200
    data = resp.json()
    ids_in_order = [q["id"] for q in data]
    assert ids_in_order.index(qids[1]) < ids_in_order.index(qids[0])
    assert ids_in_order.index(qids[0]) < ids_in_order.index(qids[2])


def test_top_monthly_returns_at_most_5(client):
    for i in range(7):
        resp = client.post(
            "/questions",
            json={
                "text": f"Q{i}",
                "resolution_criteria": "X.",
                "proposed_resolution_date": _future_iso(),
            },
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
        qid = resp.json()["id"]
        for j in range(i + 1):
            client.post(
                f"/questions/{qid}/vote",
                json={"vote": 1},
                headers={"X-Forwarded-For": f"99.0.0.{j}"},
            )

    resp = client.get("/questions/top-monthly")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 5
    # Highest first
    assert data[0]["text"] == "Q6"
    assert data[-1]["text"] == "Q2"


def test_admin_required_for_approve(client, caller_headers, admin_headers):
    create = client.post(
        "/questions",
        json={
            "text": "Q",
            "resolution_criteria": "X.",
            "proposed_resolution_date": _future_iso(),
        },
        headers=caller_headers,
    )
    qid = create.json()["id"]

    # No auth → 403
    no_auth = client.post(f"/questions/{qid}/approve", json={})
    assert no_auth.status_code == 403

    # Wrong key → 403
    wrong_auth = client.post(
        f"/questions/{qid}/approve",
        json={},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert wrong_auth.status_code == 403

    # Correct → 200
    ok = client.post(f"/questions/{qid}/approve", json={}, headers=admin_headers)
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved"


def test_approve_can_override_resolution_criteria(
    client, caller_headers, admin_headers
):
    create = client.post(
        "/questions",
        json={
            "text": "Q",
            "resolution_criteria": "vague",
            "proposed_resolution_date": _future_iso(30),
        },
        headers=caller_headers,
    )
    qid = create.json()["id"]

    new_date = _future_iso(90)
    resp = client.post(
        f"/questions/{qid}/approve",
        json={"resolution_date": new_date, "resolution_criteria": "precise criteria"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["resolution_criteria"] == "precise criteria"
