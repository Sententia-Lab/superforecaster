"""Durable execution for forecast runs, via DBOS.

A run is six agent invocations with a live search budget. Losing all of it because the
fourth one hit a usage limit is the expensive kind of failure, and the one most likely to
happen. Every agent call goes through `agent_step` below, which records it as a DBOS
workflow step, so resuming re-runs only the step that failed rather than paying for the
graph again. Wrapping the *run* in a workflow is not enough on its own: with no steps
inside it there is nothing to replay, and "resume" silently re-runs everything.

This replaces a hand-rolled checkpoint module. The thing that module existed to work
around — `pydantic_graph`'s `FileStatePersistence` marking a failed snapshot `'error'`
and then refusing to load it back, so a failed run was a post-mortem rather than a
checkpoint — is simply not a problem DBOS has.

**Resume is in-process.** `executor_id` is unique per process, so a restart does not
adopt the previous process's pending workflows: `RunRegistry` is in-memory, so a
recovered run would have no event stream, no subscribers, and nothing to report to.
`db.init_db` already marks those rows `lost` on boot, which is the honest answer. Making
recovery survive a restart means giving the workflow serializable arguments and
rebuilding the run from its database row — a change to this module and `runs.execute`,
and nothing else.
"""

from __future__ import annotations

import uuid

from config import get_settings
from dbos import DBOS, DBOSConfig

_launched = False


def configure() -> None:
    """Start DBOS. Idempotent, and safe to call from a test that never runs a workflow.

    Called from the API lifespan only. A one-shot CLI forecast or an eval has nothing to
    resume into, so it runs the same graph without the workflow layer — see `is_active`.
    """
    global _launched
    if _launched:
        return

    config: DBOSConfig = {
        "name": "superforecaster",
        "system_database_url": get_settings().dbos_database_url,
        # Unique per process: see the module docstring. This is what makes resume
        # in-process rather than cross-restart.
        "executor_id": f"local-{uuid.uuid4().hex[:8]}",
        "run_admin_server": False,
        "enable_otlp": False,
        "log_level": "WARNING",
    }
    DBOS(config=config)
    DBOS.launch()
    _launched = True


def shutdown() -> None:
    """Tear DBOS down and go back to the un-checkpointed path.

    Exists for tests. DBOS is process-global and its thread pool is bound to the event
    loop that launched it, so a test module that starts it and does not stop it leaves
    every later test taking the durable branch against a loop that has since closed —
    which fails as "cannot schedule new futures after shutdown", a long way from the
    cause.
    """
    global _launched
    if not _launched:
        return
    DBOS.destroy()
    _launched = False


def is_active() -> bool:
    """Whether runs are being checkpointed.

    Durability is a capability, not a requirement. A one-shot CLI forecast, an eval, and
    a test all execute the same graph without it — there is nothing to resume into,
    because nothing is watching and nobody will ask. `runs.execute` reads this to decide
    whether to wrap the graph in a workflow, so the un-durable path is the same code with
    one less layer rather than a second implementation.
    """
    return _launched


def workflow_id(run_id: str) -> str:
    """One DBOS workflow per run, named after it so resume needs no second lookup."""
    return f"forecast-{run_id}"


@DBOS.step(name="agent_call")
async def _durable_agent_call(fn, *args, **kwargs):
    """The registered step. One per agent invocation.

    DBOS records only the *return* value of a step, never its arguments — verified, and
    load-bearing here: `ForecastDeps` carries a live `emit` callable and the agents carry
    model clients, none of which could survive serialization. What comes back is a
    Pydantic model, which does.
    """
    return await fn(*args, **kwargs)


async def agent_step(fn, *args, **kwargs):
    """Run one agent call, durably when this process is checkpointing.

    Called by every step in the forecast graph instead of invoking the agent directly.
    Without this the whole run is a single workflow with no steps inside it, and
    "resume" re-runs the entire graph — which is precisely the bug this replaced.

    `fn` is resolved by the caller at call time from its module globals, so a test that
    monkeypatches `run_decompose` still gets the durable wrapper around its stub.
    """
    if not is_active():
        return await fn(*args, **kwargs)
    return await _durable_agent_call(fn, *args, **kwargs)


async def resume_from_failure(wf_id: str) -> tuple[str, object]:
    """Restart a failed workflow at the step that failed, keeping the earlier ones.

    `resume_workflow` is the wrong primitive: on a workflow that reached a terminal
    error it replays the recorded exception and executes nothing, so a failed run could
    never recover. `fork_workflow` restarts from a given step index, replaying the
    completed steps from their recorded results — which is the actual "re-run only the
    agent that died" behaviour.

    Forking mints a new workflow id, so the caller has to remember the one it got back;
    a second failure has to fork from the fork.
    """
    steps = await DBOS.list_workflow_steps_async(wf_id)
    # `start_step` is a `function_id`, not a list index — the fork keeps every checkpoint
    # with `function_id < start_step`. Read the id off the record rather than counting,
    # because ids are 1-based and a list index is not.
    failed = next((s for s in steps if s.get("error") is not None), None)
    start = failed["function_id"] if failed else len(steps) + 1

    handle = await DBOS.fork_workflow_async(wf_id, start_step=start)
    return handle.workflow_id, handle
