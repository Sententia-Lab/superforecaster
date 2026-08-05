"""Live run endpoints — start a forecast, then watch it think.

Starting a run is admin-gated for the same reason `POST /forecasts` is: it is five
agent invocations with a live search budget. Watching one is public — a stream costs
a queue.

The transport is server-sent events rather than WebSockets. The data is one-directional
(the only client-to-server message is "cancel", which is a DELETE), SSE reconnects
itself via `Last-Event-ID`, and it survives ordinary HTTP proxies. `sse-starlette`
supplies the framing, the keep-alive pings, and disconnect detection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from superforecaster import runs
from superforecaster.models import (
    CreateRunRequest,
    ForecastInput,
    ResumeRunRequest,
    RunEvent,
    RunSnapshot,
    RunSummary,
)

from .deps import require_admin

router = APIRouter(prefix="/runs", tags=["runs"])

HEARTBEAT_SECONDS = 15.0
"""A `:` comment this often. Idle proxies close silent connections, and a forecast
stage can legitimately spend a minute inside one search."""


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: CreateRunRequest, _: None = Depends(require_admin)
) -> RunSummary:
    """Start a forecast run in the background.

    202 rather than 201: nothing exists yet but a job. The client then opens
    `GET /runs/{id}/stream`. A 429 means every slot is busy — the UI parks the question
    in its own backlog and offers it again when one frees up.

    `async` is load-bearing: `runs.start` calls `asyncio.create_task`, which needs a
    running loop. A sync handler would run in FastAPI's threadpool and have none.
    """
    try:
        run = runs.start(
            ForecastInput(
                question=body.question,
                resolution_criteria=body.resolution_criteria,
                resolution_date=body.resolution_date,
                category=body.category,
                max_iterations=body.max_iterations,
            ),
            resolution_source=body.resolution_source,
        )
    except runs.SlotsFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        )
    return run.summary()


@router.get("")
def list_runs(limit: int = 20) -> list[RunSummary]:
    """Active runs first, then recently finished. Backs the home rail."""
    return [r.summary() for r in runs.registry.recent(limit=limit)]


@router.get("/{run_id}")
def get_run(run_id: str, from_seq: int = Query(default=0, ge=0)) -> RunSnapshot:
    """The full buffered event list — the no-SSE fallback and the debugging view."""
    run = runs.registry.get(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )
    return run.snapshot(from_seq)


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    from_seq: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """Server-sent events for one run: buffered replay, then the live tail.

    `Last-Event-ID` wins over `from_seq` so a browser's automatic reconnect resumes
    without the client tracking anything itself.
    """
    run = runs.registry.get(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found"
        )

    start_at = from_seq
    if last_event_id is not None:
        try:
            start_at = int(last_event_id) + 1
        except ValueError:
            pass

    return EventSourceResponse(
        _events(run, start_at),
        ping=HEARTBEAT_SECONDS,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this an nginx in front will buffer the whole stream into one
            # response, which turns a live view into a very slow page load.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_run(
    run_id: str,
    body: ResumeRunRequest | None = None,
    _: None = Depends(require_admin),
) -> RunSummary:
    """Re-run a failed run from its last completed node.

    Only the node that died runs again — everything before it keeps the result it was
    already paid for. `max_iterations` raises the search budget, which is the point
    when the failure was `UsageLimitExceeded`: resuming with the old budget would walk
    into the same wall.

    The event stream continues rather than restarting, so a client watching from
    `?from_seq=` sees the resume as more of the same run.
    """
    try:
        run = runs.resume_run(
            run_id, max_iterations=body.max_iterations if body else None
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return run.summary()


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_run(run_id: str, _: None = Depends(require_admin)) -> None:
    if not runs.registry.cancel(run_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="run not found or already finished",
        )


async def _events(run: runs.Run, from_seq: int) -> AsyncIterator[ServerSentEvent]:
    """Replay the buffer, then tail.

    Subscribing BEFORE snapshotting the buffer is load-bearing: the other order drops
    any event emitted in the gap between the two, which is exactly the window a busy
    run is most likely to emit in.

    Heartbeats and client-disconnect detection belong to `EventSourceResponse` — it
    sends a comment every `ping` seconds and cancels this generator when the socket
    goes, which is what the hand-rolled `wait_for` loop here used to do by hand.
    """
    queue = run.subscribe()
    try:
        seen = from_seq - 1
        for event in run.replay(from_seq):
            seen = max(seen, event.seq)
            yield _sse(event)

        if run.is_terminal and queue.empty():
            return

        while True:
            event = await queue.get()
            if event.seq <= seen:
                continue  # already replayed from the buffer
            seen = event.seq
            yield _sse(event)
            if event.type == "end":
                return
    finally:
        run.unsubscribe(queue)


def _sse(event: RunEvent) -> ServerSentEvent:
    """One frame.

    `id` is the sequence number, which is what makes `Last-Event-ID` resumption work.
    The payload goes through `model_dump_json`, so no newline inside a string can break
    the framing.
    """
    return ServerSentEvent(
        id=str(event.seq), event="run", data=event.model_dump_json()
    )
