"""Live forecast runs — registry, event buffer, and the projection of graph state
into the events a UI renders.

Three things happen here and nothing else does them:

1. **A run is a background task**, not a blocked HTTP request. `POST /runs` returns in
   milliseconds; the graph keeps going whether or not anyone is watching.
2. **Typed state becomes events.** Every `project_*` function below reads a field an
   agent already returns — `Decomposition.sub_claims`, `InsideView.adjustments`,
   `OutsideView.reference_classes`. No prompt was changed to make the UI possible, so
   what is displayed is what the methodology produced, not a second narration of it.
3. **Events fan out to subscribers** over an in-memory queue per SSE connection.

Runs are memory-resident on purpose. The final `Forecast` persists through the
existing `db.save_forecast`; the reasoning trail does not persist at all. A restart
therefore loses every in-flight run, and `db.init_db` marks those rows `lost` on boot
so the UI can say so instead of hanging on a stream that will never produce a frame.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from config import get_settings, resolve_agent_model

from . import checkpoints, checks, db
from .agents.synthesize import retry_brief
from .graphs.forecast import (
    MAX_SYNTHESIS_ATTEMPTS,
    STAGE_KEYS,
    run_forecast_graph,
)
from .graphs.state import ForecastState
from .models import (
    Decomposition,
    Forecast,
    ForecastInput,
    InsideView,
    OutsideView,
    RunEvent,
    RunSnapshot,
    RunStatus,
    RunSummary,
)

THOUGHT_FLUSH_SECONDS = 0.08
"""Token deltas are coalesced into at most one frame per interval.

Un-coalesced, a run emits one SSE frame per token — thousands of frames each carrying
three bytes of payload inside ninety bytes of envelope.
"""

SUBSCRIBER_QUEUE_SIZE = 512
"""Per-connection backlog. A client that falls this far behind is dropped and resumes
from the buffer with `?from_seq=`, rather than being allowed to stall the graph."""

STAGE_ORDER: tuple[str, ...] = ("decompose", "outside", "inside", "synth", "critique")

_TERMINAL: frozenset[str] = frozenset({"done", "error", "cancelled", "lost"})


class SlotsFullError(RuntimeError):
    """Raised when every concurrent run slot is taken."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- The run ----------


@dataclass
class Run:
    """One forecast graph execution, its event buffer, and its subscribers.

    Also the `GraphHooks` implementation — `stage_started` / `stage_finished` are why
    this class is what gets handed to `run_forecast_graph`.
    """

    id: str
    input: ForecastInput
    resolution_source: str = ""
    status: RunStatus = "queued"
    stage: str = ""
    attempt: int = 1
    seq: int = 0
    dropped: int = 0
    tool_calls: int = 0
    forecast_id: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    events: deque[RunEvent] = field(default_factory=deque)
    task: asyncio.Task[None] | None = None

    state: ForecastState | None = None
    """The graph state, kept so the final `result` event can build its waterfall.

    `run_forecast_graph` returns the forecast and its violations but not the state, and
    the waterfall needs the outside view's anchor and the inside view's adjustments.
    Holding the reference the hooks are already handed is cheaper than widening that
    return type for one caller.
    """

    _subscribers: set[asyncio.Queue[RunEvent]] = field(default_factory=set)
    _thought: str = ""
    _thought_deadline: float = 0.0

    def __post_init__(self) -> None:
        if self.events.maxlen is None:
            self.events = deque(self.events, maxlen=get_settings().run_event_buffer)

    # ---------- emit ----------

    def emit(self, type: str, payload: dict[str, Any] | None = None) -> RunEvent:
        """Append an event and fan it out. Synchronous and never blocks.

        A slow or dead subscriber cannot stall the graph: the put is non-blocking, and
        a full queue costs that one connection rather than the event. The dropped
        client reconnects and replays the gap from the buffer.
        """
        if type != "thought":
            self.flush_thought()
        return self._append(type, payload or {})

    def emit_thought(self, delta: str) -> None:
        """Buffer a token delta, flushing at most every `THOUGHT_FLUSH_SECONDS`."""
        now = time.monotonic()
        if not self._thought:
            self._thought_deadline = now + THOUGHT_FLUSH_SECONDS
        self._thought += delta
        if now >= self._thought_deadline:
            self.flush_thought()

    def flush_thought(self) -> None:
        """Emit whatever `emit_thought` buffered.

        Called before every non-thought event as well as on the timer, so narration can
        never arrive after the tool call or result it preceded.
        """
        if not self._thought:
            return
        text, self._thought = self._thought, ""
        self._append("thought", {"delta": text})

    def _append(self, type: str, payload: dict[str, Any]) -> RunEvent:
        self.seq += 1
        if len(self.events) == self.events.maxlen:
            self.dropped += 1
        event = RunEvent(
            seq=self.seq,
            run_id=self.id,
            type=type,
            stage=self.stage,
            attempt=self.attempt,
            ts=utc_now(),
            payload=payload,
        )
        self.events.append(event)

        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(q)
        return event

    # ---------- GraphHooks ----------

    def stage_started(self, stage: str, attempt: int) -> None:
        self.flush_thought()
        self.stage, self.attempt = stage, attempt
        self.emit("stage", {"stage": stage, "attempt": attempt})

        # A second synthesis is a correction, not a re-roll — but only if you can see
        # what changed. This emits the actual prompt text attempt 2 receives.
        if stage == "synth" and attempt > 1 and self.state is not None:
            if self.state.outside and self.state.inside:
                self.emit(
                    "brief",
                    retry_brief(
                        self.state.outside, self.state.inside, self.state.violations
                    ),
                )

    def stage_finished(self, stage: str, state: ForecastState) -> None:
        """Project whatever field this node just wrote onto the state."""
        self.flush_thought()
        self.state = state
        if stage == "decompose" and state.decomposition is not None:
            project_decompose(self, state.decomposition)
        elif stage == "outside" and state.outside is not None:
            assert state.decomposition is not None
            project_outside(
                self, state.decomposition, state.outside, state.sources_seen
            )
        elif stage == "inside" and state.inside is not None:
            project_inside(self, state.inside)
        elif stage == "synth" and state.forecast is not None:
            project_synth(self, state, state.synthesis_attempts)
        elif stage == "critique":
            project_critique(self, state)

    # ---------- subscribers ----------

    def subscribe(self) -> asyncio.Queue[RunEvent]:
        q: asyncio.Queue[RunEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[RunEvent]) -> None:
        self._subscribers.discard(q)

    def replay(self, from_seq: int = 0) -> list[RunEvent]:
        """Buffered events with `seq >= from_seq`.

        Prepends a `truncated` event when the requested point has already been evicted,
        so a reconnecting client learns it has a hole rather than silently rendering a
        timeline that is missing its middle.
        """
        buffered = [e for e in self.events if e.seq >= from_seq]
        oldest = self.events[0].seq if self.events else from_seq
        if self.dropped and from_seq < oldest:
            gap = RunEvent(
                seq=max(0, from_seq),
                run_id=self.id,
                type="truncated",
                ts=utc_now(),
                payload={"dropped_before_seq": oldest, "count": self.dropped},
            )
            return [gap, *buffered]
        return buffered

    # ---------- views ----------

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
            last_seq=self.seq,
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

    In-memory, which means one uvicorn worker. Two workers would each see half the
    runs, and a stream opened on the wrong one would replay nothing. The fix when that
    becomes real is a shared bus behind `Run.emit` / `Run.subscribe`, not a different
    shape here.
    """

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def create(self, input: ForecastInput, resolution_source: str = "") -> Run:
        """Allocate a run and record it. Does not start it.

        Raises `SlotsFullError` past `RUN_MAX_CONCURRENT` — a run is five agent
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
        """Drop finished runs past `RUN_RETENTION_MINUTES`.

        Called from `create` rather than on a timer — a dict cleanup does not need its
        own scheduler job, and the only moment it matters is when a new run arrives.
        """
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

    A task cancelled before the event loop ever gives it a slice has its coroutine
    closed rather than entered, so no cleanup inside `execute` runs. Without this the
    run sits `queued` forever and every stream opened on it waits for an `end` frame
    that is never coming.
    """
    if run.is_terminal:
        return
    run.status = "cancelled"
    run.ended_at = utc_now()
    db.finish_run(run.id, status=run.status, error="cancelled before start")
    run.emit("error", {"message": "run cancelled before it started"})
    run.emit("end", {"status": run.status, "forecast_id": None})


async def execute(run: Run, *, resume: bool = False) -> None:
    """Drive the graph, persist the forecast, close the stream.

    Terminal in every branch. A client cannot tell a hung server from a silently
    crashed one, so this always emits a last frame saying which happened.

    Every run is checkpointed around each node. On failure the checkpoint is left in
    place, which is what lets `resume_run` re-run only the agent that died instead of
    paying for the whole graph again.
    """
    run.status = "running"
    run.error = None
    try:
        forecast, violations = await run_forecast_graph(
            run.input,
            hooks=run,
            emit=_emitter(run),
            persistence=checkpoints.persistence_for(run.id),
            resume=resume,
        )
        forecast_id = db.save_forecast(
            forecast, resolution_source=run.resolution_source
        )
        run.forecast_id = forecast_id
        run.status = "done"
        run.stage = ""
        # Nothing can need the checkpoint once the forecast is saved.
        checkpoints.drop_checkpoint(run.id)
        run.emit(
            "result",
            result_payload(run, forecast, violations, forecast_id),
        )
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
                # What survived, so the offer to resume is concrete rather than a
                # button the user has to trust.
                "resumable": checkpoints.has_checkpoint(run.id),
                "completed_stages": [
                    STAGE_KEYS.get(n, n) for n in checkpoints.completed_stages(run.id)
                ],
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

    A usage-limit failure is the one worth special-casing: resuming with the same
    budget re-runs the same agent into the same wall, so the offer to resume has to
    come with the reason it would otherwise fail again.
    """
    if type(exc).__name__ == "UsageLimitExceeded":
        return (
            "The research budget ran out mid-node. Resume with a higher search depth, "
            "or raise RESEARCH_TOOL_CALLS_PER_ITERATION."
        )

    if type(exc).__name__ == "ModelHTTPError":
        status = getattr(exc, "status_code", None)
        model = getattr(exc, "model_name", "") or resolve_agent_model()
        if status == 404:
            # Not a bad model id, usually. The gateway routes per-model, so a model
            # that exists upstream still 404s when this account has no route for it.
            return (
                f"The provider has no route for '{model}'. This is a gateway or "
                f"account configuration problem rather than a bad run — the model id "
                f"can be valid and still 404 if your gateway has no route enabled for "
                f"it. Set AGENT_MODEL to a model your account can reach, then resume."
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
    """Re-run a failed run from its last completed node.

    Raises `LookupError` when there is no checkpoint to resume from — a run that
    finished, or one whose checkpoint was cleaned up.

    `max_iterations` overrides the search budget, because the most common reason to be
    here is that the old one was too small.
    """
    run = registry.get(run_id)
    if run is None:
        raise LookupError(f"unknown run {run_id}")
    if not run.is_terminal:
        raise ValueError("run is still going")

    node = checkpoints.rewind_for_resume(run_id)
    if node is None:
        raise LookupError("no checkpoint to resume from")

    if max_iterations is not None:
        run.input = run.input.model_copy(update={"max_iterations": max_iterations})

    # The trail continues rather than restarting: seq keeps counting, so a client
    # watching from `?from_seq=` sees the resume as more of the same run.
    run.status = "queued"
    run.ended_at = None
    run.emit(
        "resume",
        {
            "from_node": STAGE_KEYS.get(node, node),
            "completed_stages": [
                STAGE_KEYS.get(n, n) for n in checkpoints.completed_stages(run_id)
            ],
            "max_iterations": run.input.max_iterations,
        },
    )
    run.task = asyncio.create_task(execute(run, resume=True))
    run.task.add_done_callback(lambda _t: _finalize_if_orphaned(run))
    return run


def _emitter(run: Run):
    """The `deps.emit` callable. Routes thoughts through the coalescing buffer and
    counts tool calls so the header can show a running total."""

    def emit(type: str, payload: dict[str, Any]) -> None:
        if type == "thought":
            run.emit_thought(payload.get("delta", ""))
            return
        if type == "query":
            run.tool_calls += 1
        run.emit(type, payload)

    return emit


# ---------- Projections: typed state -> events ----------


def project_decompose(run: Run, d: Decomposition) -> None:
    """P1 + P2 — one `sub` per sub-claim, then how they combine."""
    for s in d.sub_claims:
        run.emit(
            "sub",
            {
                "id": s.id,
                "question": s.question,
                "p": s.probability,
                "knowability": s.knowability,
                "rationale": s.rationale,
            },
        )
    run.emit("note", {"label": "chain_note", "text": d.chain_note})


def _class_payload(rc: Any, seen: dict[str, Any]) -> dict[str, Any]:
    """One reference class, with each cited source resolved against what was fetched.

    `seen` maps URL -> SourceRef. The join is what makes "which search found this base
    rate" answerable: a class cites a URL, and the retrieved record for that URL knows
    the query that returned it and the title the result gave itself. Neither is
    something the agent could assert — it is recorded by the tool.
    """
    sources = []
    for s in rc.sources:
        ref = seen.get(s.url or "")
        sources.append(
            {
                **s.model_dump(),
                "title": getattr(ref, "title", "") if ref else "",
                "query": getattr(ref, "query", "") if ref else "",
                "retrieved": ref is not None,
            }
        )
    queries = sorted({d["query"] for d in sources if d["query"]})
    return {
        "name": rc.name,
        "rate": rc.base_rate,
        "n": rc.sample_size,
        "weight": rc.weight,
        "support": checks.claim_support(rc.sources),
        "sources": sources,
        "queries": queries,
        "analogs": [
            {"description": a.description, "outcome": a.outcome, "relevance": a.relevance}
            for a in rc.analogs
        ],
    }


def group_by_sub_claim(
    d: Decomposition, o: OutsideView, seen: Iterable[Any] = ()
) -> list[dict[str, Any]]:
    """The outside view arranged under the sub-claims it was sent to research.

    A flat list of reference classes cannot answer the question a reader actually has —
    *which part of this did you look up, and what did you find?* The grouping is a
    projection of `ReferenceClass.sub_claim_ids`, a field the agent fills in, so the UI
    is reading a relationship the backend asserted rather than inventing one.

    `rate` is `checks.sub_claim_rate`, the same weighted arithmetic `check_aggregation`
    applies to the whole question. A sub-claim nothing researched carries `rate: None`
    rather than a fabricated number — which for a `judgment` sub-claim is the correct
    answer, and for a `researchable` one is a visible gap.

    Classes claiming no sub-claim land in a final group with `id: None`: legitimate for a
    lens on the whole question, and worth seeing when it is really an unattributed rate.
    """
    by_url = {ref.url: ref for ref in seen if getattr(ref, "url", "")}
    groups = [
        {
            "id": s.id,
            "question": s.question,
            "knowability": s.knowability,
            "rationale": s.rationale,
            "rate": checks.sub_claim_rate(s.id, o),
            "classes": [
                _class_payload(rc, by_url) for rc in checks.classes_for(s.id, o)
            ],
        }
        for s in d.sub_claims
    ]

    claimed = {cid for rc in o.reference_classes for cid in rc.sub_claim_ids}
    loose = [
        rc
        for rc in o.reference_classes
        if not rc.sub_claim_ids or not (set(rc.sub_claim_ids) & claimed)
    ]
    if loose:
        groups.append(
            {
                "id": None,
                "question": "The question as a whole",
                "knowability": "researchable",
                "rationale": "",
                "rate": None,
                "classes": [_class_payload(rc, by_url) for rc in loose],
            }
        )
    return groups


def project_outside(
    run: Run, d: Decomposition, o: OutsideView, seen: Iterable[Any] = ()
) -> None:
    """P4 + P7 — what was researched for each sub-claim, and the anchor.

    Grouped rather than flat: see `group_by_sub_claim`. Everything here still lands in
    one burst, because this runs on `stage_finished` — the live progress a reader sees
    during research is the `query` and `source` events, not these.

    `disagreement` is what the aggregate note says when it is non-empty. That sentence
    is the half of P7 the schema cannot enforce, and putting it in the UI is the point
    of having demanded it.
    """
    for group in group_by_sub_claim(d, o, seen):
        run.emit("claim", group)

    pct = round(o.aggregate_base_rate * 100)
    run.emit(
        "note",
        {
            "label": f"aggregate_base_rate — {pct}%",
            "text": o.disagreement.strip()
            or f"{len(o.reference_classes)} reference classes, broadly in agreement.",
        },
    )


def project_inside(run: Run, i: InsideView) -> None:
    """P5, P9, P14, P15 — signed moves, the opposing case, and the bias sweep."""
    for a in i.adjustments:
        run.emit(
            "adj",
            {
                "evidence": a.evidence,
                "dir": a.direction,
                "mag": 0.0 if a.is_noise else a.magnitude,
                "flip": a.flip_test,
                "noise": a.is_noise,
                "sub_claim_ids": a.sub_claim_ids,
                "support": checks.claim_support(a.sources),
                "sources": [s.model_dump() for s in a.sources],
            },
        )
    run.emit("note", {"label": "steel_man", "text": i.steel_man})
    run.emit(
        "note",
        {"label": "what_would_change_my_mind", "text": i.what_would_change_my_mind},
    )
    for b in i.bias_checks:
        run.emit("bias", {"bias": b.bias, "assessment": b.assessment})


def project_synth(run: Run, state: ForecastState, attempt: int) -> None:
    """P6, P8, P16 — the number.

    `ok` is None: whether this draft survives is the critique's verdict, which lands as
    the `check` events immediately after.

    `support` is derived from the graded sources behind the reference classes and
    adjustments, not read off the forecast — the model has no field to assert it in,
    which is the point.
    """
    f = state.forecast
    assert f is not None
    support = (
        checks.aggregate_source_confidence(state.outside, state.inside)
        if state.outside and state.inside
        else None
    )
    run.emit(
        "draft",
        {
            "p": f.probability,
            "ok": None,
            "support": support,
            "note": f"Attempt {attempt}"
            + (f" · {support} evidential support." if support else "."),
        },
    )


def project_critique(run: Run, state: ForecastState) -> None:
    """Every check, passes included, then the retry banner when routing back.

    `retrying` is derived from the same condition the `Critique` node itself uses, not
    guessed — if the two ever disagree the UI would claim a retry that never happened.
    """
    if state.forecast is None or state.outside is None or state.inside is None:
        return
    assert state.decomposition is not None

    results = checks.run_forecast_checks_detailed(
        state.forecast,
        state.decomposition,
        state.outside,
        state.inside,
        sources_seen=state.sources_seen,
    )
    for r in results:
        run.emit(
            "check",
            {
                "check": r.label,
                "name": r.name,
                "ok": r.passed,
                "principle": r.principle,
                # False marks an advisory verdict — worth reading, but it did not send
                # the forecast back. Without it the UI cannot tell a warning from a
                # failure, since both arrive as ok=false.
                "blocking": r.violation.blocking if r.violation else None,
                "detail": r.violation.detail if r.violation else "",
                # The numbers the verdict was reached on, pass or fail — so the check
                # can be argued with rather than only believed.
                "evidence": r.evidence,
            },
        )

    blocking = [r for r in results if r.violation and r.violation.blocking]
    if blocking and state.synthesis_attempts < MAX_SYNTHESIS_ATTEMPTS:
        n = len(blocking)
        run.emit(
            "route",
            {
                "text": f"{n} blocking violation{'s' if n != 1 else ''}. Routing back "
                f"to Synthesize — attempt {state.synthesis_attempts + 1} of "
                f"{MAX_SYNTHESIS_ATTEMPTS}, with the violation in the prompt."
            },
        )


# ---------- The result ----------


def build_waterfall(
    o: OutsideView, i: InsideView, f: Forecast
) -> list[dict[str, Any]]:
    """Anchor -> signed adjustments -> stated, as running totals.

    Uses `checks.signed_adjustment`, so this chart and `check_derivation` can never
    disagree about what the evidence implies. The gap between the last adjustment's
    running total and the final row is the derivation slack the critique measured — it
    is visible here rather than explained.
    """
    rows: list[dict[str, Any]] = [
        {
            "label": f"Outside view anchor — {len(o.reference_classes)} reference "
            f"class{'es' if len(o.reference_classes) != 1 else ''}",
            "delta": None,
            "running": o.aggregate_base_rate,
            "kind": "anchor",
        }
    ]

    running = o.aggregate_base_rate
    for a in i.adjustments:
        delta = checks.signed_adjustment(a)
        if delta == 0.0:
            continue
        running = min(1.0, max(0.0, running + delta))
        rows.append(
            {
                "label": a.evidence,
                "delta": delta,
                "running": running,
                "kind": "up" if delta > 0 else "down",
            }
        )

    rows.append(
        {
            "label": "Stated probability",
            "delta": round(f.probability - running, 4),
            "running": f.probability,
            "kind": "final",
        }
    )
    return rows


def result_payload(
    run: Run,
    forecast: Forecast,
    violations: Iterable[Any],
    forecast_id: str,
) -> dict[str, Any]:
    """The `result` event. Everything the saved-forecast card needs, in one frame."""
    outside = run.state.outside if run.state else None
    inside = run.state.inside if run.state else None
    waterfall = build_waterfall(outside, inside, forecast) if outside and inside else []
    return {
        "forecast_id": forecast_id,
        "question": forecast.question,
        "probability": forecast.probability,
        "anchor": outside.aggregate_base_rate if outside else None,
        "support": (
            checks.aggregate_source_confidence(outside, inside)
            if outside and inside
            else None
        ),
        "reasoning": forecast.reasoning,
        "waterfall": waterfall,
        "violations": [
            v.model_dump() if hasattr(v, "model_dump") else v for v in violations
        ],
    }
