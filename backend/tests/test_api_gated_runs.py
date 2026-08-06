"""The /runs API: CRUD statuses, the start gate, and the step stream.

The stream tests are the load-bearing ones — they pin the connection-as-lifetime
contract (ADR 46): frames arrive typed, disconnect cancels, errors reach the wire.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from api.main import app
from superforecaster import db, machine, stages

from .gated_factories import decomposition, future

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


FULL_BODY = {
    "question": "Will it happen?",
    "resolution_criteria": "It observably happens.",
    "resolution_source": "the registry",
    "resolution_date": "2027-01-01T00:00:00Z",
}


async def _make_run(client, **overrides) -> dict:
    resp = await client.post("/runs", json={**FULL_BODY, **overrides})
    assert resp.status_code == 201
    return resp.json()


# ---------- CRUD ----------


async def test_create_and_list_group_by_status(client):
    run = await _make_run(client)
    backlog_only = await _make_run(client, question="Half-formed")

    listed = (await client.get("/runs")).json()
    by_id = {r["id"]: r for r in listed}
    assert by_id[run["id"]]["status"] == "backlog"
    assert by_id[backlog_only["id"]]["status"] == "backlog"


async def test_get_returns_the_full_tree(client):
    run = await _make_run(client)
    await client.post(f"/runs/{run['id']}/start")
    detail = (await client.get(f"/runs/{run['id']}")).json()
    assert detail["status"] == "active"
    assert [s["stage"] for s in detail["steps"]] == ["decompose"]


async def test_patch_only_in_backlog(client):
    run = await _make_run(client)
    resp = await client.patch(f"/runs/{run['id']}", json={"question": "Sharper?"})
    assert resp.status_code == 200

    await client.post(f"/runs/{run['id']}/start")
    resp = await client.patch(f"/runs/{run['id']}", json={"question": "Too late"})
    assert resp.status_code == 409


async def test_delete_removes_run(client):
    run = await _make_run(client)
    assert (await client.delete(f"/runs/{run['id']}")).status_code == 204
    assert (await client.get(f"/runs/{run['id']}")).status_code == 404


# ---------- the start gate ----------


async def test_start_422_names_the_missing_fields(client):
    run = await _make_run(client, resolution_source="", resolution_criteria="")
    resp = await client.post(f"/runs/{run['id']}/start")
    assert resp.status_code == 422
    assert "resolution_criteria" in resp.json()["detail"]
    assert "resolution_source" in resp.json()["detail"]


async def test_start_409_when_not_backlog(client):
    run = await _make_run(client)
    assert (await client.post(f"/runs/{run['id']}/start")).status_code == 202
    assert (await client.post(f"/runs/{run['id']}/start")).status_code == 409


# ---------- the step stream ----------


@pytest.fixture
def stub_decompose(monkeypatch):
    async def fake(input, deps):
        deps.emit and deps.emit("thought", {"delta": "thinking..."}, None)
        return decomposition()

    monkeypatch.setattr(stages, "run_decompose_stage", fake)


async def _started_run(client) -> dict:
    run = await _make_run(client)
    resp = await client.post(f"/runs/{run['id']}/start")
    return resp.json()


def _frames(sse_text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in sse_text.splitlines()
        if line.startswith("data: ") and line != "data: "
    ]


async def test_stream_yields_progress_then_result_then_run(client, stub_decompose):
    detail = await _started_run(client)
    step_id = detail["steps"][0]["id"]

    resp = await client.post(f"/runs/{detail['id']}/steps/{step_id}/stream")
    assert resp.status_code == 200
    frames = _frames(resp.text)
    types = [f["type"] for f in frames]

    assert "thought" in types
    assert types[-2:] == ["result", "run"]
    result = frames[types.index("result")]["payload"]["step"]
    assert result["status"] == "complete"
    assert result["payload"]["sub_claims"]
    run_frame = frames[-1]["payload"]
    assert any(s["stage"] == "lenses" for s in run_frame["steps"])


async def test_stream_404_and_409_preflights(client, stub_decompose):
    detail = await _started_run(client)
    step_id = detail["steps"][0]["id"]

    resp = await client.post(f"/runs/{detail['id']}/steps/not-a-step/stream")
    assert resp.status_code == 404

    await client.post(f"/runs/{detail['id']}/steps/{step_id}/stream")
    resp = await client.post(f"/runs/{detail['id']}/steps/{step_id}/stream")
    assert resp.status_code == 409  # already complete


async def test_stream_409_when_gate_not_satisfied(client, stub_decompose):
    detail = await _started_run(client)
    db.insert_steps(detail["id"], [("synthesis", "", "")])
    synth = next(
        s for s in db.list_steps(detail["id"]) if s["stage"] == "synthesis"
    )
    resp = await client.post(f"/runs/{detail['id']}/steps/{synth['id']}/stream")
    assert resp.status_code == 409
    assert "gate not satisfied" in resp.json()["detail"]


async def test_agent_error_reaches_the_wire_and_the_chip(client, monkeypatch):
    async def explode(input, deps):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(stages, "run_decompose_stage", explode)
    detail = await _started_run(client)
    step_id = detail["steps"][0]["id"]

    resp = await client.post(f"/runs/{detail['id']}/steps/{step_id}/stream")
    frames = _frames(resp.text)
    assert frames[-1]["type"] == "error"
    assert "provider exploded" in frames[-1]["payload"]["message"]
    assert "provider exploded" in db.get_gated_run(detail["id"])["error"]


async def test_disconnect_cancels_the_step(client, monkeypatch):
    """The ADR 46 contract: the client hanging up lands the step as cancelled."""
    started = asyncio.Event()

    async def hang(input, deps):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(stages, "run_decompose_stage", hang)
    detail = await _started_run(client)
    step_id = detail["steps"][0]["id"]

    async def consume():
        async with client.stream(
            "POST", f"/runs/{detail['id']}/steps/{step_id}/stream"
        ) as resp:
            async for _line in resp.aiter_lines():
                pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Give the server-side generator a beat to unwind.
    for _ in range(50):
        step = db.get_step(step_id)
        if step["status"] == "error":
            break
        await asyncio.sleep(0.02)

    assert step["status"] == "error"
    assert step["error"] == "cancelled"
    assert db.claim_step(step_id) is not None
    assert not machine.busy()
