"""Durable execution for forecast runs, via DBOS.

A run is six agent invocations with a live search budget. Losing all of it because the
fourth one hit a usage limit is the expensive kind of failure, and the one most likely to
happen. DBOS records every agent call as a workflow step, so resuming re-runs only the
step that failed rather than paying for the graph again.

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

    Called from `db.init_db` so every entry point — API, CLI, cron — gets it without
    each having to remember.
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
