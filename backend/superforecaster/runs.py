"""Live forecast runs — registry, lifecycle, and the wire.

Three things happen here and nothing else does them:

1. **A run is a background task**, not a blocked HTTP request. `POST /runs` returns in
   milliseconds; the graph keeps going whether or not anyone is watching.
2. **A run is durable.** Every agent call goes through `durability.agent_step`, which
   makes it a DBOS step, so a failed run resumes from the agent that died rather than
   re-paying for the whole graph. Resuming *forks* the workflow at the failed step —
   `resume_workflow` would replay the recorded error and run nothing.
3. **Events fan out to subscribers**, buffered for replay. See `eventstream`.

What is deliberately *not* here any more: a projection layer. The graph emits the typed
objects its agents already returned — a whole `Decomposition`, a whole `OutsideView` —
and the frontend decides what to draw. Reshaping those into display dicts was six
hundred lines of backend code encoding layout decisions, and every one of them was a
place the UI and the methodology could drift apart.

Runs are memory-resident on purpose. The final `Forecast` persists through
`db.save_forecast`; the reasoning trail does not persist server-side at all. A restart
therefore loses every in-flight run, and `db.init_db` marks those rows `lost` on boot so
the UI can say so instead of hanging on a stream that will never produce a frame.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from config import get_settings, resolve_agent_model
from dbos import DBOS, SetWorkflowID

from . import db, durability
from .eventstream import SUBSCRIBER_QUEUE_SIZE, EventStream
from .graphs.forecast import STAGE_ORDER
from .graphs.state import ForecastState
from .models import ForecastInput, RunEvent, RunSnapshot, RunStatus, RunSummary

_TERMINAL: frozenset[str] = frozenset({"done", "error", "cancelled", "lost"})


class SlotsFullError(RuntimeError):
    """Raised when every concurrent run slot is taken."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- The run ----------


@dataclass
class Run:
    """One forecast execution: its status, its event stream, and its graph state."""

    id: str
    input: ForecastInput
    resolution_source: str = ""
    status: RunStatus = "queued"
    stage: str = ""
    attempt: int = 1
    tool_calls: int = 0
    forecast_id: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    task: asyncio.Task[None] | None = None

    workflow_id: str | None = None
    """The DBOS workflow currently backing this run.

    Not derivable from `id`: resuming forks the workflow, which mints a new id, and a
    second failure has to fork from the fork rather than from the original.
    """

    state: ForecastState | None = None
    """The graph state, held so a resumed run keeps writing to the same object."""

    stream: EventStream[RunEvent] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.stream is None:
            self.stream = EventStream(
                buffer=get_settings().run_event_buffer,
                queue_size=SUBSCRIBER_QUEUE_SIZE,
            )

    # ---------- emit ----------

    def emit(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        sub_claim: str | None = None,
    ) -> RunEvent:
        """Append an event and fan it out. Synchronous and never blocks."""
        return self.stream.publish(
            lambda seq: self._event(seq, type, payload or {}, sub_claim), sub_claim
        )

    def emit_thought(self, delta: str, sub_claim: str | None = None) -> None:
        self.stream.publish_delta(
            delta,
            sub_claim,
            lambda seq, text, key: self._event(seq, "thought", {"delta": text}, key),
        )

    def _event(
        self, seq: int, type: str, payload: dict[str, Any], sub_claim: str | None
    ) -> RunEvent:
        return RunEvent(
            seq=seq,
            run_id=self.id,
            type=type,
            stage=self.stage,
            attempt=self.attempt,
            sub_claim=sub_claim,
            ts=utc_now(),
            payload=payload,
        )

    # ---------- subscribers ----------

    def subscribe(self) -> asyncio.Queue[RunEvent]:
        return self.stream.subscribe()

    def unsubscribe(self, q: asyncio.Queue[RunEvent]) -> None:
        self.stream.unsubscribe(q)

    def replay(self, from_seq: int = 0) -> list[RunEvent]:
        """Buffered events with `seq >= from_seq`.

        Prepends a `truncated` event when the requested point has already been evicted,
        so a reconnecting client learns it has a hole rather than silently rendering a
        timeline that is missing its middle.
        """
        buffered = list(self.stream.replay(from_seq, lambda e: e.seq))
        oldest = self.stream.oldest_seq(lambda e: e.seq) or from_seq
        if self.stream.dropped and from_seq < oldest:
            gap = RunEvent(
                seq=max(0, from_seq),
                run_id=self.id,
                type="truncated",
                ts=utc_now(),
                payload={"dropped_before_seq": oldest, "count": self.stream.dropped},
            )
            return [gap, *buffered]
        return buffered

    # ---------- views ----------

    @property
    def seq(self) -> int:
        return self.stream.seq

    def flush_thought(self, sub_claim=None) -> None:
        """Emit whatever narration is buffered. No argument means every column."""
        if sub_claim is None:
            self.stream.flush()
        else:
            self.stream.flush(sub_claim)

    @property
    def dropped(self) -> int:
        return self.stream.dropped

    @property
    def events(self):
        """The buffered events, oldest first. Read-only view for tests and snapshots."""
        return self.stream.events

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def summary(self) -> RunSummary:
        return RunSummary(
            id=self.id,
            question=self.input.question,
            status=self.status,
            stage=self.stage,
            stage_index=(
                STAGE_ORDER.index(self.stage) + 1 if self.stage in STAGE_ORDER else 0
            ),
            attempt=self.attempt,
            tool_calls=self.tool_calls,
            last_seq=self.stream.seq,
            forecast_id=self.forecast_id,
            error=self.error,
            created_at=self.created_at,
            ended_at=self.ended_at,
            max_iterations=self.input.max_iterations,
        )

    def snapshot(self, from_seq: int = 0) -> RunSnapshot:
        return RunSnapshot(summary=self.summary(), events=self.replay(from_seq))


# ---------- Registry ----------


class RunRegistry:
    """Process-wide run table.

    In-memory, which means one uvicorn worker. Two workers would each see half the runs,
    and a stream opened on the wrong one would replay nothing. The fix when that becomes
    real is a shared bus behind `Run.emit` / `Run.subscribe`, not a different shape here.
    """

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def create(self, input: ForecastInput, resolution_source: str = "") -> Run:
        """Allocate a run and record it. Does not start it.

        Raises `SlotsFullError` past `RUN_MAX_CONCURRENT` — a run is six agent
        invocations with a live search budget, so the cap is about cost, not memory.
        """
        self.reap()
        limit = get_settings().run_max_concurrent
        if len(self.active()) >= limit:
            raise SlotsFullError(f"all {limit} run slots are busy")

        run = Run(
            id=f"run_{uuid.uuid4().hex[:7]}",
            input=input,
            resolution_source=resolution_source,
        )
        self._runs[run.id] = run
        db.create_run(
            run_id=run.id,
            question=input.question,
            resolution_criteria=input.resolution_criteria,
            resolution_source=resolution_source,
            resolution_date=input.resolution_date,
            category=input.category,
        )
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def active(self) -> list[Run]:
        return [r for r in self._runs.values() if not r.is_terminal]

    def recent(self, limit: int = 20) -> list[Run]:
        """Active runs first, then finished, newest first within each group."""
        ordered = sorted(
            self._runs.values(),
            key=lambda r: (r.is_terminal, -r.created_at.timestamp()),
        )
        return ordered[:limit]

    def slots_free(self) -> int:
        return max(0, get_settings().run_max_concurrent - len(self.active()))

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.is_terminal or run.task is None:
            return False
        run.task.cancel()
        return True

    def reap(self) -> int:
        """Drop finished runs past `RUN_RETENTION_MINUTES`."""
        cutoff = utc_now() - timedelta(minutes=get_settings().run_retention_minutes)
        stale = [
            rid
            for rid, r in self._runs.items()
            if r.is_terminal and (r.ended_at or r.created_at) < cutoff
        ]
        for rid in stale:
            del self._runs[rid]
        return len(stale)

    def clear(self) -> None:
        """Drop everything. Tests only."""
        self._runs.clear()


registry = RunRegistry()


# ---------- Driving a run ----------


def start(input: ForecastInput, resolution_source: str = "") -> Run:
    """Create a run and schedule it. Returns as soon as the task exists."""
    run = registry.create(input, resolution_source)
    run.task = asyncio.create_task(execute(run))
    run.task.add_done_callback(lambda _t: _finalize_if_orphaned(run))
    return run


def _finalize_if_orphaned(run: Run) -> None:
    """Close a run whose task ended without `execute` reaching its `finally`.

    A task cancelled before the event loop ever gives it a slice has its coroutine closed
    rather than entered, so no cleanup inside `execute` runs. Without this the run sits
    `queued` forever and every stream opened on it waits for an `end` frame that is never
    coming.
    """
    if run.is_terminal:
        return
    run.status = "cancelled"
    run.ended_at = utc_now()
    db.finish_run(run.id, status=run.status, error="cancelled before start")
    run.emit("error", {"message": "run cancelled before it started"})
    run.emit("end", {"status": run.status, "forecast_id": None})


async def _run_forecast(run_id: str) -> None:
    """Drive the graph for one run.

    Takes only the run id because everything else — the emit sink, the graph state — is
    live objects that cannot be serialized. That is also why resume is in-process; see
    `durability`.
    """
    from .graphs.forecast import run_forecast_graph

    run = registry.get(run_id)
    assert run is not None, f"workflow for unknown run {run_id}"

    if run.state is None:
        run.state = ForecastState(input=run.input)

    forecast, violations = await run_forecast_graph(
        run.input, emit=_emitter(run), state=run.state
    )
    forecast_id = db.save_forecast(forecast, resolution_source=run.resolution_source)
    run.forecast_id = forecast_id
    run.emit(
        "result",
        {
            "forecast_id": forecast_id,
            "forecast": forecast.model_dump(),
            "violations": [v.model_dump() for v in violations],
        },
    )


_forecast_workflow = DBOS.workflow()(_run_forecast)
"""The durable wrapper. The agent calls inside it are steps — see `durability.agent_step`.

Kept separate from `_run_forecast` because the decorator refuses to be *called* at all
before DBOS is initialized, so the un-durable path needs the undecorated function rather
than a flag checked inside it.
"""


async def execute(run: Run, *, resume: bool = False) -> None:
    """Drive the graph, persist the forecast, close the stream.

    Terminal in every branch. A client cannot tell a hung server from a silently crashed
    one, so this always emits a last frame saying which happened.
    """
    run.status = "running"
    run.error = None
    try:
        if not durability.is_active():
            # No checkpointing configured — same graph, one less layer. See
            # `durability.is_active`.
            await _run_forecast(run.id)
        elif resume:
            assert run.workflow_id is not None, "resume with nothing to resume from"
            run.workflow_id, handle = await durability.resume_from_failure(
                run.workflow_id
            )
            await handle.get_result()
        else:
            run.workflow_id = durability.workflow_id(run.id)
            with SetWorkflowID(run.workflow_id):
                await _forecast_workflow(run.id)
        run.status = "done"
        run.stage = ""
    except asyncio.CancelledError:
        run.status = "cancelled"
        run.emit("error", {"message": "run cancelled"})
        raise
    except Exception as exc:  # noqa: BLE001 — the message is the product here
        run.status = "error"
        run.error = f"{type(exc).__name__}: {exc}"
        run.emit(
            "error",
            {
                "message": run.error,
                # DBOS keeps the completed steps, so resuming re-runs only what failed.
                "resumable": durability.is_active(),
                "hint": _failure_hint(exc),
            },
        )
    finally:
        run.ended_at = utc_now()
        db.finish_run(
            run.id,
            status=run.status,
            forecast_id=run.forecast_id,
            error=run.error,
        )
        run.emit("end", {"status": run.status, "forecast_id": run.forecast_id})


def _failure_hint(exc: Exception) -> str:
    """What a caller should change before resuming, when the failure says so.

    A usage-limit failure is the one worth special-casing: resuming with the same budget
    re-runs the same agent into the same wall, so the offer to resume has to come with
    the reason it would otherwise fail again.
    """
    if type(exc).__name__ == "UsageLimitExceeded":
        return (
            "Every column ran out of searches, so nothing was researched. Resume with a "
            "higher search depth, or raise CELL_SOFT_CALLS_PER_ITERATION / "
            "CELL_HARD_HEADROOM."
        )

    if type(exc).__name__ == "ModelHTTPError":
        status = getattr(exc, "status_code", None)
        model = getattr(exc, "model_name", "") or resolve_agent_model()
        if status == 404:
            # Not a bad model id, usually. The gateway routes per-model, so a model that
            # exists upstream still 404s when this account has no route for it.
            return (
                f"The provider has no route for '{model}'. This is a gateway or account "
                f"configuration problem rather than a bad run — the model id can be "
                f"valid and still 404 if your gateway has no route enabled for it. Set "
                f"AGENT_MODEL to a model your account can reach, then resume."
            )
        if status in (401, 403):
            return (
                "The provider rejected the credentials. Check "
                "PYDANTIC_AI_GATEWAY_API_KEY or ANTHROPIC_API_KEY, then resume."
            )
        if status == 429:
            return "Rate limited by the provider. Wait, then resume."
        if status is not None and status >= 500:
            return "The provider had a server error. Resuming re-runs only this step."
    return ""


def resume_run(run_id: str, *, max_iterations: int | None = None) -> Run:
    """Re-run a failed run from its last completed step.

    `max_iterations` overrides the search budget, because the most common reason to be
    here is that the old one was too small.
    """
    run = registry.get(run_id)
    if run is None:
        raise LookupError(f"unknown run {run_id}")
    if not run.is_terminal:
        raise ValueError("run is still going")
    if durability.is_active() and run.workflow_id is None:
        raise LookupError("no checkpoint to resume from")

    if max_iterations is not None:
        run.input = run.input.model_copy(update={"max_iterations": max_iterations})
        if run.state is not None:
            # The graph reads the budget off `state.input`, not the caller's. Without
            # this, resuming with a higher depth re-runs into the same wall while the UI
            # reports the depth that was asked for.
            run.state.input = run.input

    # The trail continues rather than restarting: seq keeps counting, so a client
    # watching from `?from_seq=` sees the resume as more of the same run.
    run.status = "queued"
    run.ended_at = None
    run.emit("resume", {"max_iterations": run.input.max_iterations})
    run.task = asyncio.create_task(execute(run, resume=True))
    run.task.add_done_callback(lambda _t: _finalize_if_orphaned(run))
    return run


def _emitter(run: Run):
    """The `deps.emit` callable handed down to the graph and its agents.

    Stage boundaries are events like any other; this is the only place that reads them,
    because the run header needs to know which stage is live.
    """

    def emit(type: str, payload: dict[str, Any], sub_claim: str | None = None) -> None:
        if type == "thought":
            run.emit_thought(payload.get("delta", ""), sub_claim)
            return
        if type == "stage":
            run.stream.flush()
            run.stage = payload.get("stage", "")
            run.attempt = int(payload.get("attempt", 1))
        elif type == "stage_end":
            run.stream.flush()
        elif type == "query":
            # A plain `+=` from the single event loop thread, so concurrent columns are
            # still safe — there is no await between the read and the write.
            run.tool_calls += 1
        run.emit(type, payload, sub_claim)

    return emit
