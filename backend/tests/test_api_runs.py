"""Integration tests for /runs — the streaming endpoints.

The graph is stubbed. What is verified is the transport contract: auth, the 429 when
slots are full, SSE framing, and that a reconnect resumes at the right place rather
than replaying a timeline the client already has.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from superforecaster import runs
from tests.test_graph_forecast import stub_agents  # noqa: F401 — pytest fixture

ADMIN_KEY = "test-admin-key"


@pytest.fixture(autouse=True)
def _set_admin_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)


@pytest.fixture(autouse=True)
def _clean_registry():
    runs.registry.clear()
    yield
    runs.registry.clear()


@asynccontextmanager
async def _noop_lifespan(app):
    from superforecaster import db

    db.init_db()
    yield


@pytest.fixture
def client():
    """A client whose portal outlives a single request.

    Without the `with`, Starlette starts and tears down the event loop per request,
    which cancels the background run task partway through. That was invisible while a
    stubbed run finished inside the request; checkpoint file I/O yields, so it no
    longer does.
    """
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def start_run(client, admin_headers, **overrides) -> str:
    """POST a run and wait for it to reach a terminal state."""
    body = {**a_body(), **overrides}
    run_id = client.post("/runs", json=body, headers=admin_headers).json()["id"]
    return wait_for(client, run_id)


def wait_for(client, run_id: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = runs.registry.get(run_id)
        if run is not None and run.is_terminal:
            return run_id
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def a_body() -> dict:
    return {
        "question": "Will X happen?",
        "resolution_criteria": "X is observable.",
        "resolution_date": (
            datetime.now(timezone.utc) + timedelta(days=60)
        ).isoformat(),
        "category": "test",
    }


def parse_sse(text: str) -> list[dict]:
    """Frames out of an SSE body, ignoring heartbeats."""
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


# ---------- creating ----------


def test_create_requires_admin(client):
    assert client.post("/runs", json=a_body()).status_code == 403


def test_create_returns_202_and_a_queued_summary(client, admin_headers, stub_agents):  # noqa: F811
    res = client.post("/runs", json=a_body(), headers=admin_headers)

    assert res.status_code == 202
    body = res.json()
    assert body["id"].startswith("run_")
    assert body["status"] in ("queued", "running", "done")
    assert body["question"] == "Will X happen?"


def test_create_returns_429_when_every_slot_is_busy(
    client, admin_headers, monkeypatch, stub_agents  # noqa: F811
):
    monkeypatch.setenv("RUN_MAX_CONCURRENT", "1")
    runs.registry.create(runs.ForecastInput(**{**a_body(), "max_iterations": 5}))

    res = client.post("/runs", json=a_body(), headers=admin_headers)
    assert res.status_code == 429
    assert "slot" in res.json()["detail"]


# ---------- reading ----------


def test_list_runs_is_public(client, admin_headers, stub_agents):  # noqa: F811
    client.post("/runs", json=a_body(), headers=admin_headers)

    res = client.get("/runs")
    assert res.status_code == 200
    assert len(res.json()) == 1


def test_get_run_returns_a_snapshot_with_events(client, admin_headers, stub_agents):  # noqa: F811
    run_id = start_run(client, admin_headers)

    body = client.get(f"/runs/{run_id}").json()
    assert body["summary"]["id"] == run_id
    assert [e["type"] for e in body["events"]][:1] == ["stage"]


def test_get_unknown_run_is_404(client):
    assert client.get("/runs/run_nope").status_code == 404


# ---------- streaming ----------


def test_stream_replays_a_finished_run_and_terminates(
    client, admin_headers, stub_agents  # noqa: F811
):
    """A completed run must still stream — the client may arrive after it landed."""
    run_id = start_run(client, admin_headers)

    res = client.get(f"/runs/{run_id}/stream")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(res.text)
    assert [e["type"] for e in events][-2:] == ["result", "end"]
    assert all(e["run_id"] == run_id for e in events)


def test_frames_carry_the_seq_as_the_sse_id(client, admin_headers, stub_agents):  # noqa: F811
    """`id:` is what makes Last-Event-ID resumption work."""
    run_id = start_run(client, admin_headers)

    text = client.get(f"/runs/{run_id}/stream").text
    ids = [int(l[len("id: ") :]) for l in text.splitlines() if l.startswith("id: ")]
    events = parse_sse(text)

    assert ids == [e["seq"] for e in events]
    assert ids == sorted(ids)


def test_from_seq_resumes_without_replaying_the_start(
    client, admin_headers, stub_agents  # noqa: F811
):
    run_id = start_run(client, admin_headers)

    everything = parse_sse(client.get(f"/runs/{run_id}/stream").text)
    resumed = parse_sse(client.get(f"/runs/{run_id}/stream?from_seq=5").text)

    assert len(resumed) == len(everything) - 4
    assert resumed[0]["seq"] == 5


def test_last_event_id_header_wins_over_from_seq(
    client, admin_headers, stub_agents  # noqa: F811
):
    """The browser sends the header on its own reconnect; the client tracks nothing."""
    run_id = start_run(client, admin_headers)

    resumed = parse_sse(
        client.get(
            f"/runs/{run_id}/stream?from_seq=0", headers={"Last-Event-ID": "6"}
        ).text
    )
    assert resumed[0]["seq"] == 7


def test_stream_of_a_crashed_run_ends_with_error_then_end(
    client, admin_headers, monkeypatch, stub_agents  # noqa: F811
):
    from superforecaster.graphs import forecast as fg

    async def boom(*a, **kw):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(fg, "run_decompose", boom)
    run_id = start_run(client, admin_headers)

    events = parse_sse(client.get(f"/runs/{run_id}/stream").text)
    assert [e["type"] for e in events][-2:] == ["error", "end"]
    assert "provider exploded" in events[-2]["payload"]["message"]


def test_stream_of_an_unknown_run_is_404(client):
    assert client.get("/runs/run_nope/stream").status_code == 404


# ---------- cancelling ----------


def test_cancel_requires_admin(client, admin_headers, stub_agents):  # noqa: F811
    run_id = start_run(client, admin_headers)
    assert client.delete(f"/runs/{run_id}").status_code == 403


def test_cancelling_a_finished_run_is_404(client, admin_headers, stub_agents):  # noqa: F811
    run_id = start_run(client, admin_headers)
    res = client.delete(f"/runs/{run_id}", headers=admin_headers)
    assert res.status_code == 404


# ---------- local mode ----------


def test_a_test_client_is_not_local_mode(client):
    """Why the auth tests above still 403 without a token: TestClient's client host is
    `testclient`, not loopback, so it takes the deployed path."""
    assert client.get("/config").json()["auth_required"] is True


def test_config_is_public(client):
    res = client.get("/config")
    assert res.status_code == 200
    assert set(res.json()) >= {"auth_required", "search_enabled", "model"}


def test_admin_is_skipped_for_an_unauthenticated_request_from_localhost(monkeypatch):
    """The one-command case: export an API key, start the server, click Run now."""
    from fastapi import Request

    from api import deps

    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 51234)}
    assert deps.is_local_mode(Request(scope)) is True


def test_a_configured_key_always_wins(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "secret")
    from fastapi import Request

    from api import deps

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 51234)}
    assert deps.is_local_mode(Request(scope)) is False


def test_a_forwarded_request_is_never_local_mode(monkeypatch):
    """A reverse proxy in front of this is the shape of a real deployment, and anything
    upstream can write whatever it likes into the client address."""
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    from fastapi import Request

    from api import deps

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"203.0.113.9")],
        "client": ("127.0.0.1", 51234),
    }
    assert deps.is_local_mode(Request(scope)) is False


def test_a_remote_request_with_no_key_is_a_clear_500(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    from fastapi import Request

    from api import deps

    scope = {"type": "http", "headers": [], "client": ("203.0.113.9", 51234)}
    assert deps.is_local_mode(Request(scope)) is False
