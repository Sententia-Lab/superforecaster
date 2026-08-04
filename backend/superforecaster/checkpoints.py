"""Graph checkpoints — so a failed run resumes instead of starting over.

A forecast run is five agent invocations with a live search budget. Losing all of it
because the fourth one hit a usage limit is the expensive kind of failure, and the one
most likely to happen: `UsageLimitExceeded` fires mid-node, after the decomposition and
the base rates have already been paid for.

`pydantic_graph` snapshots state before and after every node, which is exactly the
granularity that matters here — a node is one agent call, so "resume from the last
snapshot" means "re-run only the agent that failed".

One wrinkle this module exists to handle. `FileStatePersistence` marks a snapshot
`'error'` when its node raises, and `load_next` only returns snapshots with status
`'created'`. A failed run is therefore not resumable as-is: `iter_from_persistence`
would find nothing and raise. `rewind_for_resume` flips the stalled snapshot back to
`'created'`, which is the whole difference between a checkpoint and a post-mortem.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import get_settings
from pydantic_graph.persistence.file import FileStatePersistence

# Statuses a snapshot can be stuck in. 'error' is a node that raised; 'pending' and
# 'running' are a node interrupted by a process death, which leaves no exception behind
# but is equally unfinished.
_RESUMABLE = ("error", "pending", "running")


def checkpoint_dir() -> Path:
    return Path(get_settings().run_checkpoint_dir)


def checkpoint_path(run_id: str) -> Path:
    return checkpoint_dir() / f"{run_id}.json"


def persistence_for(run_id: str) -> FileStatePersistence:
    """One file per run, reused across every step of that run."""
    checkpoint_dir().mkdir(parents=True, exist_ok=True)
    return FileStatePersistence(checkpoint_path(run_id))


def has_checkpoint(run_id: str) -> bool:
    path = checkpoint_path(run_id)
    return path.is_file() and path.stat().st_size > 0


def drop_checkpoint(run_id: str) -> None:
    """Delete a run's checkpoint. Called once the run has finished successfully —
    keeping it would just accumulate state for a run that can never need it."""
    checkpoint_path(run_id).unlink(missing_ok=True)


def _read(run_id: str) -> list[dict]:
    try:
        data = json.loads(checkpoint_path(run_id).read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write(run_id: str, snapshots: list[dict]) -> None:
    checkpoint_path(run_id).write_text(json.dumps(snapshots, indent=1))


def completed_stages(run_id: str) -> list[str]:
    """Node names that already ran successfully, oldest first.

    Read from the raw JSON rather than through `FileStatePersistence.load_all`, which
    needs the graph's types bound and would make this import `graphs` — the dependency
    runs the other way.
    """
    return [
        s["node"]["node_id"]
        for s in _read(run_id)
        if s.get("kind") == "node"
        and s.get("status") == "success"
        and isinstance(s.get("node"), dict)
    ]


def rewind_for_resume(run_id: str) -> str | None:
    """Make a stalled run resumable, and say which node will re-run.

    Resets the newest snapshot in a non-terminal state back to `'created'`. Everything
    before it keeps its recorded result, so resuming re-runs exactly one agent — the
    one that failed — rather than the whole graph.

    Returns the node id that will re-run, or None when there is nothing to resume:
    either no checkpoint, or a run that actually finished.
    """
    snapshots = _read(run_id)
    if not snapshots:
        return None

    for snapshot in reversed(snapshots):
        if snapshot.get("kind") != "node":
            continue
        if snapshot.get("status") in _RESUMABLE:
            snapshot["status"] = "created"
            snapshot.pop("start_ts", None)
            snapshot.pop("duration", None)
            _write(run_id, snapshots)
            node = snapshot.get("node") or {}
            return node.get("node_id")
        if snapshot.get("status") == "created":
            # Already resumable — a process that died between snapshotting a node and
            # starting it.
            node = snapshot.get("node") or {}
            return node.get("node_id")

    return None
