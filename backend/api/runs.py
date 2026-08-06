"""Gated run endpoints — the sidebar CRUD plus the step stream.

The load-bearing decision here is ADR 46: `POST .../steps/{id}/stream` *is* the step's
execution. The SSE response runs the agent inside its own generator, so the connection
is the agent's lifetime — the client hanging up (laptop closed, tab gone) cancels the
generator, which cancels the step, which lands it as `error='cancelled'` in the
database, immediately claimable again. There is no background task registry, no ring
buffer, no replay, and no watchdog, because there is nothing running that nobody is
watching.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from sse_starlette.sse import EventSourceResponse

from superforecaster import db, machine
from superforecaster.errors import AgentTimeout, StageTimeout
from superforecaster.models import (
    MAX_SEARCH_DEPTH,
    CreateGatedRunRequest,
    GatedRunDetail,
    GatedRunSummary,
    UpdateGatedRunRequest,
)

from .deps import require_admin

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


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(run_id: str, _: None = Depends(require_admin)) -> None:
    try:
        db.delete_gated_run(run_id)
    except db.NotFoundError as exc:
        raise _404(str(exc))


@router.post("/{run_id}/start", status_code=status.HTTP_202_ACCEPTED)
def start_run(run_id: str, _: None = Depends(require_admin)) -> GatedRunDetail:
    """The four-field gate, then `backlog → active` with a pending decompose step.

    Nothing executes here — the decompose step sits pending until its stream is
    opened. 422 on missing fields so the UI can say exactly which ones.
    """
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


def _failure_hint(exc: BaseException) -> str:
    """One honest sentence about why the step died, with the fix when there is one."""
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
    _: None = Depends(require_admin),
) -> EventSourceResponse:
    """The gated "next": execute one step, streaming its progress until it lands.

    Frames are `data:` JSON with a `type` field — `thought`/`query`/`source`/
    `exhausted` while the agent works, then `result` (the finished step) and `run`
    (the updated tree, including any newly materialized pending steps), or `error`.
    Disconnecting cancels the step (ADR 46).
    """
    step = db.get_step(step_id)
    if step is None or step["run_id"] != run_id:
        raise _404(f"step {step_id} not found on run {run_id}")
    run = db.get_gated_run(run_id)
    if run is None:
        raise _404(f"run {run_id}")
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

        def emit(type: str, payload: dict, sub_claim: str | None = None) -> None:
            # Must stay synchronous and non-blocking — it is called from inside the
            # agent's event handler, where an await would stall token delivery.
            queue.put_nowait({"type": type, "sub_claim": sub_claim, "payload": payload})

        async def work() -> None:
            try:
                finished = await machine.execute_step(
                    step_id, max_iterations=max_iterations, emit=emit
                )
                payload = (
                    json.loads(finished["payload_json"])
                    if finished.get("payload_json")
                    else None
                )
                queue.put_nowait(
                    {
                        "type": "result",
                        "payload": {
                            "step": {
                                k: v
                                for k, v in finished.items()
                                if k != "payload_json"
                            }
                            | {"payload": payload},
                        },
                    }
                )
                queue.put_nowait({"type": "run", "payload": machine.detail(run_id)})
            except asyncio.CancelledError:
                raise
            except (machine.GateError, machine.BusyError, db.NotFoundError) as exc:
                queue.put_nowait({"type": "error", "payload": {"message": str(exc)}})
            except Exception as exc:  # noqa: BLE001 — every failure must reach the wire
                queue.put_nowait(
                    {"type": "error", "payload": {"message": _failure_hint(exc)}}
                )
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(work())
        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    break
                yield {"data": json.dumps(frame, default=str)}
        finally:
            # The client hung up (or the loop above finished). Cancelling a finished
            # task is a no-op; cancelling a live one is exactly ADR 46.
            if not task.done():
                task.cancel()
                with suppress(BaseException):
                    await task

    return EventSourceResponse(
        generate(),
        ping=15,
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
