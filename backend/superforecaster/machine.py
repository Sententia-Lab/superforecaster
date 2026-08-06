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

from config import get_stage_timeout

from . import db, stages
from .deps import ForecastDeps
from .errors import AgentTimeout, StageTimeout
from .models import (
    BaseRateStepPayload,
    Decomposition,
    ForecastInput,
    InsideStepPayload,
    SubClaimLenses,
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


def advance(run_id: str) -> None:
    """Materialize the next stage's pending rows once the current stage is done.

    Reads the just-written payloads to know the fan-out: decompose fixes the
    sub-questions, each lenses step fixes its cells. Idempotent — `insert_steps`
    is INSERT OR IGNORE against the identity key.
    """
    steps = db.list_steps(run_id)
    by_stage: dict[str, list[dict]] = {}
    for s in steps:
        by_stage.setdefault(s["stage"], []).append(s)

    def complete(stage: str) -> bool:
        rows = by_stage.get(stage, [])
        return bool(rows) and all(s["status"] == "complete" for s in rows)

    if complete("decompose") and "lenses" not in by_stage:
        decomposition = _decomposition_of(steps)
        researchable = [
            s for s in decomposition.sub_claims if s.knowability == "researchable"
        ]
        if researchable:
            db.insert_steps(
                run_id, [("lenses", s.id or "", "") for s in researchable]
            )
        else:
            # Nothing to research: synthesis runs on the whole-question fallbacks.
            db.insert_steps(run_id, [("synthesis", "", "")])
        return

    if complete("lenses") and "base_rates" not in by_stage:
        cells = []
        for s in by_stage["lenses"]:
            lenses = SubClaimLenses.model_validate_json(s["payload_json"])
            cells.extend(
                ("base_rates", s["sub_claim_id"], lens.name) for lens in lenses.lenses
            )
        db.insert_steps(run_id, cells)
        return

    if complete("base_rates") and "inside_view" not in by_stage:
        db.insert_steps(
            run_id,
            [
                ("inside_view", s["sub_claim_id"], s["lens_name"])
                for s in by_stage["base_rates"]
            ],
        )
        return

    if complete("inside_view") and "synthesis" not in by_stage:
        db.insert_steps(run_id, [("synthesis", "", "")])
        return


def gate_offender(step: dict, steps: list[dict]) -> str | None:
    """Every step in every earlier stage must be complete. Returns the offender."""
    order = {stage: i for i, stage in enumerate(db.STAGE_ORDER)}
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

        try:
            async with asyncio.timeout(get_stage_timeout() or None):
                payload_json = await _dispatch(step, run["id"], input, deps)
        except asyncio.CancelledError:
            db.fail_step(step_id, "cancelled")
            raise
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

        finished = db.finish_step(step_id, payload_json)
        if step["stage"] == "synthesis":
            payload = SynthesisStepPayload.model_validate_json(payload_json)
            forecast_id = db.save_forecast(
                payload.forecast, resolution_source=run["resolution_source"]
            )
            db.complete_gated_run(run["id"], forecast_id)
        else:
            advance(run["id"])
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
    sub_claim = _sub_claim(decomposition, step["sub_claim_id"])

    if stage == "lenses":
        lenses = await stages.run_lenses_stage(input, decomposition, sub_claim, deps)
        return lenses.model_dump_json()

    if stage == "base_rates":
        lens = _chosen_lens(steps, step["sub_claim_id"], step["lens_name"])
        payload = await stages.run_base_rate_step(input, sub_claim, lens, deps)
        return payload.model_dump_json()

    if stage == "inside_view":
        base = _cell_payload(steps, "base_rates", step["sub_claim_id"], step["lens_name"])
        payload = await stages.run_inside_step(
            input, sub_claim, BaseRateStepPayload.model_validate_json(base), deps
        )
        return payload.model_dump_json()

    if stage == "synthesis":
        base_rate_cells = []
        inside_cells = []
        for s in steps:
            if s["status"] != "complete" or not s["payload_json"]:
                continue
            claim = _sub_claim(decomposition, s["sub_claim_id"])
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
        if s["stage"] == "decompose" and s["status"] == "complete" and s["payload_json"]:
            return Decomposition.model_validate_json(s["payload_json"])
    raise GateError("decompose has not completed")


def _sub_claim(decomposition: Decomposition, sub_claim_id: str) -> SubPrediction:
    if not sub_claim_id:
        # Whole-question steps (decompose, synthesis) have no column of their own.
        return SubPrediction(
            question=decomposition.sub_claims[0].question,
            probability=0.5,
            rationale="whole-question step",
        )
    for s in decomposition.sub_claims:
        if s.id == sub_claim_id:
            return s
    raise GateError(f"sub-claim {sub_claim_id} is not in the decomposition")


def _chosen_lens(steps: list[dict], sub_claim_id: str, lens_name: str):
    for s in steps:
        if (
            s["stage"] == "lenses"
            and s["sub_claim_id"] == sub_claim_id
            and s["status"] == "complete"
            and s["payload_json"]
        ):
            lenses = SubClaimLenses.model_validate_json(s["payload_json"])
            for lens in lenses.lenses:
                if lens.name == lens_name:
                    return lens
    raise GateError(f"lens {lens_name!r} was never chosen for {sub_claim_id}")


def _cell_payload(
    steps: list[dict], stage: str, sub_claim_id: str, lens_name: str
) -> str:
    for s in steps:
        if (
            s["stage"] == stage
            and s["sub_claim_id"] == sub_claim_id
            and s["lens_name"] == lens_name
            and s["status"] == "complete"
            and s["payload_json"]
        ):
            return s["payload_json"]
    raise GateError(f"no completed {stage} payload for ({sub_claim_id}, {lens_name})")


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
