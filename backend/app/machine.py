"""The gated-run state machine — every legal transition, in one place.

`db` reads and writes rows; this module decides. A run is `backlog → active →
complete`; a step is `pending → running → complete | error`, with `error → running`
on retry. Nothing runs unless a user asks for that specific step (ADR 45), and only
one agent step may be in flight in the whole process at a time — the budget is one
person's API key, and idle-at-a-gate costs nothing.

Every terminal write here is a plain synchronous sqlite call, so a cancelled
coroutine (the client hung up — ADR 46) still records `cancelled` on its way out.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

import logfire
from superforecaster.config import get_stage_timeout

from . import db
from superforecaster import stages
from superforecaster.stages import STAGE_ORDER
from superforecaster.agents.decompose import with_ids
from superforecaster.deps import ForecastDeps
from superforecaster.errors import AgentTimeout, StageTimeout
from superforecaster.models import (
    BaseRateStepPayload,
    Decomposition,
    ForecastInput,
    InsideStepPayload,
    SubQuestionLenses,
    SubQuestionLensesEdit,
    SubPrediction,
    SynthesisStepPayload,
)


class GateError(Exception):
    """The requested transition is not legal from where the run stands."""


class BusyError(Exception):
    """Another agent step is already in flight. One at a time, everywhere."""


_slot = asyncio.Lock()

REQUIRED_FIELDS = (
    "question",
    "resolution_criteria",
    "resolution_source",
    "resolution_date",
)


def start_run(run_id: str) -> dict:
    """The four-field gate, then `backlog → active`, then the first pending step.

    A forecast whose resolution nobody adjudicates cannot be scored, which makes
    running it a waste of the whole search budget — so all four fields are checked
    here, where the run starts, not at creation, where a half-formed backlog idea is
    legitimate.
    """
    run = db.get_gated_run(run_id)
    if run is None:
        raise db.NotFoundError(f"run {run_id}")
    missing = [f for f in REQUIRED_FIELDS if not run[f]]
    if missing:
        raise GateError(f"cannot start: missing {', '.join(missing)}")

    db.start_gated_run(run_id)
    db.insert_steps(run_id, [("decompose", "", "")])
    return detail(run_id)


Identity = tuple[str, str, str]
"""One step row's identity: (stage, sub_question_id, lens_name). The table's UNIQUE key."""


def _all_complete(steps: list[dict], keys: set[Identity]) -> bool:
    """True when every key has a row and all of those rows are complete.

    Checks keys rather than a whole stage, which covers both gaps: a row that is still
    pending, and a row an edit has not created yet.
    """
    by_key = {(s["stage"], s["sub_question_id"], s["lens_name"]): s for s in steps}
    return all(by_key.get(k, {}).get("status") == "complete" for k in keys)


def expected_steps(steps: list[dict]) -> set[Identity]:
    """The identities the completed payloads imply. Pure — reads, never writes.

    Walks the five stage names in order, asking at each one whether a finished payload
    lets it compute that stage's rows, and stops at the first stage that is not fully
    complete. So a stage's rows never appear before the stage that fans them out has
    finished.

    Two properties the callers depend on. `decompose` is always included, so its row is
    never stale. And each stage is added *before* its completeness is checked, so pending
    rows stay in the result — only rows nobody expects are left out.
    """
    out: set[Identity] = {("decompose", "", "")}
    if not _all_complete(steps, out):
        return out

    decomposition = _decomposition_of(steps)
    researchable = [
        s for s in decomposition.sub_questions if s.knowability == "researchable"
    ]
    if not researchable:
        # Nothing to research: synthesis runs on the whole-question fallbacks.
        return out | {("synthesis", "", "")}

    lenses: set[Identity] = {("lenses", s.id or "", "") for s in researchable}
    out |= lenses
    if not _all_complete(steps, lenses):
        return out

    cells: set[Identity] = set()
    for s in steps:
        # Only rows the decomposition still expects. A lens row left over from a previous
        # decomposition must not fan out cells of its own on its way to being deleted.
        if (s["stage"], s["sub_question_id"], s["lens_name"]) not in lenses:
            continue
        chosen = SubQuestionLenses.model_validate_json(s["payload_json"])
        cells.update(
            ("base_rates", s["sub_question_id"], lens.name) for lens in chosen.lenses
        )
    out |= cells
    if not _all_complete(steps, cells):
        return out

    inside: set[Identity] = {("inside_view", sc, lens) for _, sc, lens in cells}
    out |= inside
    if not _all_complete(steps, inside):
        return out

    return out | {("synthesis", "", "")}


def reconcile(run_id: str) -> None:
    """Make the step rows match `expected_steps`. The only writer, and idempotent.

    Moving forward from a finished step this does exactly what the old `advance` did:
    `expected_steps` only grows, so nothing is ever stale and the call is pure insert.
    After a payload edit it also removes the rows the new payload no longer implies.
    """
    steps = db.list_steps(run_id)
    want = expected_steps(steps)
    have = {(s["stage"], s["sub_question_id"], s["lens_name"]): s for s in steps}

    stale = [s for key, s in have.items() if key not in want]
    # Unreachable: `edit_blocker` forbids an edit once anything downstream has run. If it
    # ever is reached that is a bug, and deleting somebody's work would hide it. The raise
    # comes before both writes, so nothing is deleted and nothing is inserted.
    kept = [s for s in stale if s["status"] != "pending"]
    if kept:
        raise GateError(
            f"cannot discard {kept[0]['stage']} step {kept[0]['id']} — "
            f"it is {kept[0]['status']}"
        )

    db.delete_steps([s["id"] for s in stale])
    db.insert_steps(run_id, sorted(want - set(have)))


DERIVED: dict[str, Callable[[list[dict], dict], list[dict]]] = {
    # A decomposition with no researchable sub-questions fans out straight to synthesis, so
    # synthesis is derived directly from the decomposition too.
    "decompose": lambda steps, step: [
        s for s in steps if s["stage"] in ("lenses", "synthesis")
    ],
    # Every base rate in the run, not only this sub-question's. Populations are chosen
    # before they are measured (ADR 40), and once any rate has come back, re-choosing
    # populations *anywhere* is choosing them with a measured number in hand — the run
    # has seen an answer, so no part of it is pre-registered any more.
    "lenses": lambda steps, step: [s for s in steps if s["stage"] == "base_rates"],
}
"""Editable stage -> the rows whose existence or blindness that stage's payload owns."""


def edit_blocker(step: dict, steps: list[dict]) -> str | None:
    """None while the payload may still be edited; otherwise what already ran.

    A payload is editable exactly while everything derived from it is untouched. So an
    edit can only ever strand empty pending rows, which is why `reconcile` never has to
    decide whether some work is worth destroying.
    """
    if step["stage"] not in DERIVED:
        return f"{step['stage']} payloads are not editable"
    if step["status"] != "complete":
        return f"step is {step['status']} — there is no payload to edit"
    for derived in DERIVED[step["stage"]](steps, step):
        if derived["status"] != "pending":
            return f"{derived['stage']} step {derived['id']} is {derived['status']}"
    return None


def edit_payload(run_id: str, step_id: str, body: dict) -> dict:
    """Replace a completed payload with one a person wrote, then rebuild what it implies.

    The body is validated here rather than in the route signature because its shape
    depends on the step's stage, which is only known once the row has been read.
    """
    step = db.get_step(step_id)
    if step is None or step["run_id"] != run_id:
        raise db.NotFoundError(f"step {step_id} not found on run {run_id}")
    run = db.get_gated_run(run_id)
    if run is None:
        raise db.NotFoundError(f"run {run_id}")
    if run["status"] != "active":
        raise GateError(f"run is {run['status']}, not active")

    blocker = edit_blocker(step, db.list_steps(run_id))
    if blocker is not None:
        raise GateError(f"cannot edit: {blocker}")

    if step["stage"] == "decompose":
        # Ids are stamped by the same helper the agent's own output goes through, so a
        # hand-written decomposition and a generated one are numbered identically.
        payload = with_ids(Decomposition.model_validate(body))
    else:
        payload = SubQuestionLensesEdit.model_validate(body)

    db.edit_step_payload(step_id, payload.model_dump_json())
    reconcile(run_id)
    return detail(run_id)


def gate_offender(step: dict, steps: list[dict]) -> str | None:
    """Every step in every earlier stage must be complete. Returns the offender."""
    order = {stage: i for i, stage in enumerate(STAGE_ORDER)}
    mine = order[step["stage"]]
    for s in steps:
        if order[s["stage"]] < mine and s["status"] != "complete":
            return f"{s['stage']} step {s['id']} is {s['status']}"
    return None


async def execute_step(
    step_id: str,
    *,
    max_iterations: int | None = None,
    emit: Callable[[str, dict, str | None], None] | None = None,
) -> dict:
    """Claim and run one gated step; persist whatever happens to it.

    The caller owns the connection this runs under — cancellation (the client hung
    up) lands the step as `error='cancelled'` and re-raises, so the step is
    immediately claimable again. Everything else lands as the step's error text and
    the run's red chip.
    """
    step = db.get_step(step_id)
    if step is None:
        raise db.NotFoundError(f"step {step_id}")
    run = db.get_gated_run(step["run_id"])
    if run is None:
        raise db.NotFoundError(f"run {step['run_id']}")
    if run["status"] != "active":
        raise GateError(f"run is {run['status']}, not active")

    offender = gate_offender(step, db.list_steps(run["id"]))
    if offender is not None:
        raise GateError(f"gate not satisfied: {offender}")

    if _slot.locked():
        raise BusyError("another step is already running")

    async with _slot:
        claimed = db.claim_step(step_id)
        if claimed is None:
            raise GateError(f"step is {step['status']} and cannot be claimed twice")

        input = ForecastInput(
            question=run["question"],
            resolution_criteria=run["resolution_criteria"],
            resolution_date=run["resolution_date"],
            category=run["category"],
            max_iterations=max_iterations or run["max_iterations"],
        )
        deps = ForecastDeps(emit=emit)

        cancelled: asyncio.CancelledError | None = None
        try:
            # One span per step, so every agent run inside it — reflect plus both
            # synthesis attempts, say — nests under a single trace instead of
            # surfacing as unrelated root spans.
            with logfire.span(
                "step {stage}",
                stage=step["stage"],
                run_id=run["id"],
                step_id=step_id,
                sub_question_id=step["sub_question_id"],
                lens_name=step["lens_name"],
                attempt=claimed["attempts"],
            ) as span:
                try:
                    async with asyncio.timeout(get_stage_timeout() or None):
                        payload_json = await _dispatch(step, run["id"], input, deps)
                except asyncio.CancelledError as exc:
                    # Deliberate stop (ADR 46), not a failure: tag the span, let it
                    # close cleanly, and re-raise after — the errors view should show
                    # only real ones.
                    span.set_attribute("cancelled", True)
                    cancelled = exc
        except AgentTimeout as exc:
            # One agent stalled — its own failure, not the stage ceiling's, even
            # though both subclass TimeoutError.
            db.fail_step(step_id, f"{type(exc).__name__}: {exc}")
            raise
        except TimeoutError:
            exc = StageTimeout(
                f"stage exceeded {get_stage_timeout():.0f}s (STAGE_TIMEOUT_SECONDS)"
            )
            db.fail_step(step_id, f"{type(exc).__name__}: {exc}")
            raise exc from None
        except Exception as exc:
            db.fail_step(step_id, f"{type(exc).__name__}: {exc}")
            raise

        if cancelled is not None:
            db.fail_step(step_id, "cancelled")
            raise cancelled

        finished = db.finish_step(step_id, payload_json)
        if step["stage"] == "synthesis":
            payload = SynthesisStepPayload.model_validate_json(payload_json)
            forecast_id = db.save_forecast(
                payload.forecast, resolution_source=run["resolution_source"]
            )
            db.complete_gated_run(run["id"], forecast_id)
        else:
            reconcile(run["id"])
        return finished


async def _dispatch(
    step: dict, run_id: str, input: ForecastInput, deps: ForecastDeps
) -> str:
    """Run the right stage function with its context loaded from prior payloads."""
    steps = db.list_steps(run_id)
    stage = step["stage"]

    if stage == "decompose":
        decomposition = await stages.run_decompose_stage(input, deps)
        return decomposition.model_dump_json()

    decomposition = _decomposition_of(steps)
    sub_question = _sub_question(decomposition, step["sub_question_id"])

    if stage == "lenses":
        lenses = await stages.run_lenses_stage(input, decomposition, sub_question, deps)
        return lenses.model_dump_json()

    if stage == "base_rates":
        lens = _chosen_lens(steps, step["sub_question_id"], step["lens_name"])
        payload = await stages.run_base_rate_step(input, sub_question, lens, deps)
        return payload.model_dump_json()

    if stage == "inside_view":
        base = _cell_payload(
            steps, "base_rates", step["sub_question_id"], step["lens_name"]
        )
        payload = await stages.run_inside_step(
            input, sub_question, BaseRateStepPayload.model_validate_json(base), deps
        )
        return payload.model_dump_json()

    if stage == "synthesis":
        base_rate_cells = []
        inside_cells = []
        for s in steps:
            if s["status"] != "complete" or not s["payload_json"]:
                continue
            claim = _sub_question(decomposition, s["sub_question_id"])
            if s["stage"] == "base_rates":
                base_rate_cells.append(
                    (claim, BaseRateStepPayload.model_validate_json(s["payload_json"]))
                )
            elif s["stage"] == "inside_view":
                inside_cells.append(
                    (claim, InsideStepPayload.model_validate_json(s["payload_json"]))
                )
        payload = await stages.run_synthesis_stage(
            input, decomposition, base_rate_cells, inside_cells, deps
        )
        return payload.model_dump_json()

    raise GateError(f"unknown stage {stage!r}")


def _decomposition_of(steps: list[dict]) -> Decomposition:
    for s in steps:
        if (
            s["stage"] == "decompose"
            and s["status"] == "complete"
            and s["payload_json"]
        ):
            return Decomposition.model_validate_json(s["payload_json"])
    raise GateError("decompose has not completed")


def _sub_question(decomposition: Decomposition, sub_question_id: str) -> SubPrediction:
    if not sub_question_id:
        # Whole-question steps (decompose, synthesis) have no column of their own.
        return SubPrediction(
            question=decomposition.sub_questions[0].question,
            probability=0.5,
            rationale="whole-question step",
        )
    for s in decomposition.sub_questions:
        if s.id == sub_question_id:
            return s
    raise GateError(f"sub-question {sub_question_id} is not in the decomposition")


def _chosen_lens(steps: list[dict], sub_question_id: str, lens_name: str):
    for s in steps:
        if (
            s["stage"] == "lenses"
            and s["sub_question_id"] == sub_question_id
            and s["status"] == "complete"
            and s["payload_json"]
        ):
            lenses = SubQuestionLenses.model_validate_json(s["payload_json"])
            for lens in lenses.lenses:
                if lens.name == lens_name:
                    return lens
    raise GateError(f"lens {lens_name!r} was never chosen for {sub_question_id}")


def _cell_payload(
    steps: list[dict], stage: str, sub_question_id: str, lens_name: str
) -> str:
    for s in steps:
        if (
            s["stage"] == stage
            and s["sub_question_id"] == sub_question_id
            and s["lens_name"] == lens_name
            and s["status"] == "complete"
            and s["payload_json"]
        ):
            return s["payload_json"]
    raise GateError(
        f"no completed {stage} payload for ({sub_question_id}, {lens_name})"
    )


def detail(run_id: str) -> dict:
    """A run plus its steps with payloads parsed — what `GET /runs/{id}` returns."""
    run = db.get_gated_run(run_id)
    if run is None:
        raise db.NotFoundError(f"run {run_id}")
    steps = db.list_steps(run_id)
    for s in steps:
        s["payload"] = json.loads(s["payload_json"]) if s["payload_json"] else None
        del s["payload_json"]
    run["steps"] = steps
    return run


def busy() -> bool:
    """True while an agent step is in flight anywhere in this process."""
    return _slot.locked()
