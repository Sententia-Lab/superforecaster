"""Gated run endpoints — the sidebar CRUD plus the step stream.

`POST .../steps/{id}/stream` *is* the step's execution (ADR 46): the connection is the
agent's lifetime, so a client that hangs up cancels the step.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import ValidationError
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from sse_starlette.sse import EventSourceResponse

from app import db, machine, research
from superforecaster.errors import AgentTimeout, StageTimeout
from superforecaster.events import AgentEvent, frame
from superforecaster.config import MAX_SEARCH_DEPTH
from superforecaster.models import (
    CreateGatedRunRequest,
    GatedRunDetail,
    GatedRunSummary,
    ResearchPage,
    UpdateGatedRunRequest,
)

router = APIRouter(prefix="/runs", tags=["runs"])


def _404(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)


def _409(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_run(body: CreateGatedRunRequest) -> GatedRunDetail:
    """Create a run in the backlog. Partial fields are fine — starting is what gates."""
    run = db.create_gated_run(
        question=body.question,
        resolution_criteria=body.resolution_criteria,
        resolution_source=body.resolution_source,
        resolution_date=body.resolution_date,
        category=body.category,
        max_iterations=body.max_iterations,
    )
    return GatedRunDetail(**{**run, "steps": []})


@router.get("")
def list_runs(limit: int = Query(default=100, ge=1, le=500)) -> list[GatedRunSummary]:
    return [GatedRunSummary(**r) for r in db.list_gated_runs(limit=limit)]


@router.get("/{run_id}")
def get_run(run_id: str) -> GatedRunDetail:
    try:
        return GatedRunDetail(**machine.detail(run_id))
    except db.NotFoundError as exc:
        raise _404(str(exc))


@router.patch("/{run_id}")
def edit_run(run_id: str, body: UpdateGatedRunRequest) -> GatedRunDetail:
    try:
        db.update_gated_run_fields(
            run_id,
            question=body.question,
            resolution_criteria=body.resolution_criteria,
            resolution_source=body.resolution_source,
            resolution_date=body.resolution_date,
            category=body.category,
            max_iterations=body.max_iterations,
        )
    except db.NotFoundError as exc:
        raise _404(str(exc))
    except db.StateError as exc:
        raise _409(str(exc))
    return GatedRunDetail(**machine.detail(run_id))


@router.get("/{run_id}/research")
def run_research(
    run_id: str,
    q: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> ResearchPage:
    """What this run has read so far, ranked as `search_research` ranks it for an agent."""
    run = db.get_gated_run(run_id)
    if run is None:
        raise _404(f"run {run_id}")

    research_id = run["research_id"]
    query = (q or "").strip()
    results = (
        research.search_research(research_id, query, limit=limit, mark=True)
        if query
        else research.list_research(research_id, limit=limit)
    )
    return ResearchPage(
        total=research.count_research(research_id),
        query=query,
        results=results,
    )


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(run_id: str) -> None:
    try:
        db.delete_gated_run(run_id)
    except db.NotFoundError as exc:
        raise _404(str(exc))


@router.post("/{run_id}/start", status_code=status.HTTP_202_ACCEPTED)
def start_run(run_id: str) -> GatedRunDetail:
    """The four-field gate, then `backlog → active` with a pending decompose step."""
    try:
        return GatedRunDetail(**machine.start_run(run_id))
    except db.NotFoundError as exc:
        raise _404(str(exc))
    except machine.GateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        )
    except db.StateError as exc:
        raise _409(str(exc))


@router.put("/{run_id}/steps/{step_id}/payload")
def edit_step_payload(
    run_id: str,
    step_id: str,
    body: dict,
) -> GatedRunDetail:
    """Replace a completed payload by hand. The body's shape depends on the stage, so
    `machine.edit_payload` validates it. Returns the whole run."""
    try:
        return GatedRunDetail(**machine.edit_payload(run_id, step_id, body))
    except db.NotFoundError as exc:
        raise _404(str(exc))
    except machine.GateError as exc:
        raise _409(str(exc))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()],
        )


def _failure_hint(exc: BaseException) -> str:
    """Why the step died, with the fix when there is one."""
    if isinstance(exc, (machine.GateError, machine.BusyError, db.NotFoundError)):
        return str(exc)
    if isinstance(exc, UsageLimitExceeded):
        return (
            f"{exc} — the step ran out of search budget. Retry with a higher "
            f"max_iterations (up to {MAX_SEARCH_DEPTH})."
        )
    if isinstance(exc, AgentTimeout):
        return f"{exc} — the model stopped responding. Retrying usually clears this."
    if isinstance(exc, StageTimeout):
        return str(exc)
    if isinstance(exc, ModelHTTPError):
        if exc.status_code in (401, 403):
            return f"{exc} — the model API rejected the configured key."
        if exc.status_code == 429:
            return f"{exc} — the model API is rate-limiting. Wait a moment and retry."
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


@router.post("/{run_id}/steps/{step_id}/stream")
async def stream_step(
    run_id: str,
    step_id: str,
    max_iterations: int | None = Query(default=None, ge=1, le=MAX_SEARCH_DEPTH),
) -> EventSourceResponse:
    """Execute one step, streaming `thought`/`query`/`source`/`exhausted` frames, then
    `run` (the updated tree) or `error`. Disconnecting cancels the step."""
    step = db.get_step(step_id)
    if step is None or step["run_id"] != run_id:
        raise _404(f"step {step_id} not found on run {run_id}")
    run = db.get_gated_run(run_id)
    if run is None:
        raise _404(f"run {run_id}")
    # Checked here as well as in `execute_step` so the client gets a status code; once
    # the stream starts the status is already 200.
    if run["status"] != "active":
        raise _409(f"run is {run['status']}, not active")
    if step["status"] not in ("pending", "error"):
        raise _409(f"step is {step['status']} and cannot be started")
    offender = machine.gate_offender(step, db.list_steps(run_id))
    if offender is not None:
        raise _409(f"gate not satisfied: {offender}")
    if machine.busy():
        raise _409("another step is already running — one at a time, everywhere")

    async def generate():
        queue: asyncio.Queue = asyncio.Queue()

        def emit(event: AgentEvent, sub_question: str | None = None) -> None:
            queue.put_nowait(frame(event, sub_question))

        async def work() -> None:
            try:
                await machine.execute_step(
                    step_id, max_iterations=max_iterations, emit=emit
                )
                queue.put_nowait({"type": "run", "payload": machine.detail(run_id)})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — every failure must reach the wire
                queue.put_nowait(
                    {"type": "error", "payload": {"message": _failure_hint(exc)}}
                )
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(work())
        try:
            while (item := await queue.get()) is not None:
                yield {"data": json.dumps(item, default=str)}
        finally:
            if not task.done():
                task.cancel()
                with suppress(BaseException):
                    await task

    return EventSourceResponse(
        generate(),
        ping=15,
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
