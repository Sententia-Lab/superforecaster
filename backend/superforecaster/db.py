"""SQLite persistence layer.

All DB operations live here. The schema is defined in `init_db()` and created on first
connection. Queries are intentionally written in plain SQL — there is no ORM. Measured
against this file, one would eliminate ~200 lines of row-mapping and add ~150 of model
and migration scaffolding, and roughly 40% of what is here is forecasting domain rules an
ORM does not touch. See ADR 38-adjacent notes in the audit.

Scoring maths lives in `scoring`, not here — what a forecast is *worth* is a methodology
question, and it is testable without a database.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


# ---------- sqlite datetime adapters ----------
# Python 3.12 deprecated the default datetime adapter; it also doesn't handle
# timezone-aware datetimes correctly. Register our own that round-trip ISO 8601.


def _adapt_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _convert_timestamp(value: bytes) -> datetime:
    s = value.decode("utf-8")
    # Tolerate both " " and "T" separators
    s = s.replace(" ", "T", 1)
    return datetime.fromisoformat(s)


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_timestamp)

from config import get_settings

from . import scoring
from .models import (
    CalibrationBucket,
    CalibrationReport,
    Forecast,
    ForecastRecord,
    ForecastUpdateRecord,
    ResearchSummary,
    SubPrediction,
)


# ---------- Errors ----------


class NotFoundError(Exception):
    """Raised when a record lookup returns no rows."""


class StateError(Exception):
    """Raised on an invalid state transition (e.g. updating after resolution)."""


# ---------- Connection ----------


def _db_path() -> str:
    return get_settings().database_path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection with sane defaults.

    `journal_mode=WAL` and a `busy_timeout` are not optional here: APScheduler's daily
    refresh runs in the same process as the API, so a write during a request is ordinary
    rather than exceptional. Without WAL a reader blocks a writer; without the timeout the
    loser of that race gets `database is locked` immediately instead of waiting a moment.

    Foreign keys are enforced. Row factory returns dicts for ergonomic access.
    """
    conn = sqlite3.connect(_db_path(), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA_VERSION = 3
"""Bump this and add a step to `MIGRATIONS` whenever an existing table has to change.

`CREATE TABLE IF NOT EXISTS` covers a fresh database and nothing else. It does not add a
column, drop one, or relax a constraint — it silently does nothing to a table that already
exists, which is how `forecast_updates.confidence` survived ADR 29 deleting it everywhere
else. The INSERT stopped supplying the value; the `NOT NULL` column stayed; every run then
completed all five stages and died on the last write. A schema drift that only surfaces
after a full run is the most expensive kind there is.
"""

MIGRATIONS: dict[int, tuple[str, ...]] = {
    # ADR 29 deleted forecast-level confidence. Dropping the column rather than recreating
    # the database keeps every forecast already scored against it.
    1: ("ALTER TABLE forecast_updates DROP COLUMN confidence;",),
    # ADR 48 removed the community features; ADR 45 replaced the write-only `runs` table
    # with the gated-run machine. `DROP TABLE IF EXISTS` is idempotent, so this step is
    # safe against a fresh database whose create-block simply never made these tables.
    2: (
        "DROP TABLE IF EXISTS votes;",
        "DROP TABLE IF EXISTS questions;",
        "DROP TABLE IF EXISTS refresh_runs;",
        "DROP TABLE IF EXISTS runs;",
    ),
    # A payload a person wrote is different evidence from one the agent produced, so the
    # difference stays visible rather than being inferred from a timestamp gap.
    3: ("ALTER TABLE run_steps ADD COLUMN edited_at TIMESTAMP;",),
}
"""version -> statements that take the schema from `version - 1` to `version`.

Each step must be safe to apply to a database that reached the previous version by *either*
route: an old database being upgraded, or a fresh one just built by `init_db`'s
`CREATE TABLE` block. `_migrate` skips a step whose work the fresh schema already did, so
"drop a column that no longer exists" and "add a column the create block already made" are
both no-ops rather than errors — see `_MIGRATION_NO_OPS`.
"""

_MIGRATION_NO_OPS = ("no such column", "duplicate column name")
"""Errors that mean "the fresh schema already did this step's work", so the step is skipped.

Both directions, because a table can arrive at a step by two routes. A version-2 database
upgrading in place has `run_steps` without `edited_at`, so migration 3's ALTER runs for real.
A database stamped at version 0 with only the pre-gated tables gets `run_steps` built by the
current `CREATE TABLE` block — column included — and then the same ALTER would raise. Neither
is a failure; both leave the schema in the same correct shape.
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to `SCHEMA_VERSION`.

    `PRAGMA user_version` is a four-byte integer SQLite keeps in the file header for
    exactly this. No extra table, no dependency, and it survives a copy of the file.

    A fresh database is stamped at `SCHEMA_VERSION` by `init_db` before this runs, so
    historical steps never replay against a schema that was born correct. That matters
    more with every migration added: a step that *renames* a column would succeed against
    a fresh database and corrupt it, and no amount of tolerance inside the step would
    help. Tolerating a no-op is still worth keeping for databases stamped before this.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return

    for version in range(current + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS.get(version, ()):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as e:
                # The fresh-schema case, in both directions. Anything else is a real
                # failure and has to surface.
                if not any(n in str(e).lower() for n in _MIGRATION_NO_OPS):
                    raise
        conn.execute(f"PRAGMA user_version = {version}")


def init_db() -> None:
    """Create tables if they don't exist, then migrate. Safe to call repeatedly."""
    with connect() as conn:
        fresh = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='forecasts'"
        ).fetchone()[0] == 0

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS forecasts (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                resolution_criteria TEXT NOT NULL,
                resolution_source TEXT NOT NULL,
                category TEXT NOT NULL,
                submission_gap_days INTEGER NOT NULL DEFAULT 7,
                submission_deadline TIMESTAMP NOT NULL,
                resolution_date TIMESTAMP NOT NULL,
                resolved_at TIMESTAMP,
                outcome REAL,
                is_ambiguous INTEGER NOT NULL DEFAULT 0,
                scored_probability REAL,
                brier_score REAL,
                last_refreshed_at TIMESTAMP,
                flagged_for_resolution_review INTEGER NOT NULL DEFAULT 0,
                initial_reasoning TEXT NOT NULL,
                decompositions_json TEXT NOT NULL,
                research_json TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            );

            CREATE TABLE IF NOT EXISTS forecast_updates (
                id TEXT PRIMARY KEY,
                forecast_id TEXT NOT NULL REFERENCES forecasts(id) ON DELETE CASCADE,
                probability REAL NOT NULL,
                reasoning TEXT NOT NULL,
                is_late INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_updates_forecast ON forecast_updates(forecast_id, created_at);

            CREATE TABLE IF NOT EXISTS gated_runs (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL DEFAULT '',
                resolution_criteria TEXT NOT NULL DEFAULT '',
                resolution_source TEXT NOT NULL DEFAULT '',
                resolution_date TIMESTAMP,
                category TEXT NOT NULL DEFAULT 'general',
                max_iterations INTEGER NOT NULL DEFAULT 5,
                status TEXT NOT NULL DEFAULT 'backlog',
                error TEXT,
                forecast_id TEXT REFERENCES forecasts(id) ON DELETE SET NULL,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS ix_gated_runs_status ON gated_runs(status, created_at DESC);

            CREATE TABLE IF NOT EXISTS run_steps (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES gated_runs(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                sub_claim_id TEXT NOT NULL DEFAULT '',
                lens_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                payload_json TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                edited_at TIMESTAMP,
                UNIQUE(run_id, stage, sub_claim_id, lens_name)
            );

            CREATE INDEX IF NOT EXISTS ix_run_steps_run ON run_steps(run_id, stage);
            """
        )
        if fresh:
            # Born at the current schema, so no historical step has anything to do here.
            # Stamping now is what stops a future migration — a rename, say — from
            # "upgrading" a database that was already correct.
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _migrate(conn)

    # A step can only be `running` while a live connection is executing it. Anything
    # still marked running after a restart died with the process; saying so beats
    # leaving the UI on a spinner that will never resolve.
    mark_interrupted_steps()


# ---------- Helpers ----------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------- Forecasts ----------


def save_forecast(
    forecast: Forecast,
    resolution_source: str,
    submission_gap_days: int = 7,
) -> str:
    """Insert a new forecast plus its initial update row. Returns UUID."""
    fid = str(uuid.uuid4())
    now = _utcnow()
    submission_deadline = forecast.resolution_date - timedelta(days=submission_gap_days)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO forecasts (
                id, question, resolution_criteria, resolution_source, category,
                submission_gap_days, submission_deadline, resolution_date,
                initial_reasoning, decompositions_json, research_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fid,
                forecast.question,
                forecast.resolution_criteria,
                resolution_source,
                forecast.category,
                submission_gap_days,
                submission_deadline,
                forecast.resolution_date,
                forecast.reasoning,
                json.dumps([d.model_dump() for d in forecast.decompositions]),
                forecast.research.model_dump_json(),
                now,
            ),
        )
        # Initial update row
        conn.execute(
            """
            INSERT INTO forecast_updates (
                id, forecast_id, probability, reasoning, is_late, created_at
            ) VALUES (?, ?, ?, ?, 0, ?)
            """,
            (
                str(uuid.uuid4()),
                fid,
                forecast.probability,
                forecast.reasoning,
                now,
            ),
        )
    return fid


def add_forecast_update(
    forecast_id: str,
    probability: float,
    reasoning: str,
) -> ForecastUpdateRecord:
    """Insert a new probability update.

    Raises StateError if the forecast is already resolved or past
    resolution_date. Sets `is_late` if within 24h of resolution_date.
    """
    now = _utcnow()
    with connect() as conn:
        row = conn.execute(
            "SELECT resolution_date, outcome, is_ambiguous FROM forecasts WHERE id = ?",
            (forecast_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"forecast {forecast_id}")
        resolution_date = _ensure_aware(row["resolution_date"])
        if row["outcome"] is not None or row["is_ambiguous"]:
            raise StateError("forecast is already resolved")
        if now >= resolution_date:
            raise StateError("update deadline has passed (resolution_date)")

        is_late = (resolution_date - now) <= timedelta(hours=24)
        update_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO forecast_updates (
                id, forecast_id, probability, reasoning, is_late, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                update_id,
                forecast_id,
                probability,
                reasoning,
                int(is_late),
                now,
            ),
        )

    return ForecastUpdateRecord(
        id=update_id,
        forecast_id=forecast_id,
        probability=probability,
        reasoning=reasoning,
        is_late=is_late,
        created_at=now,
    )


def get_forecast(forecast_id: str) -> ForecastRecord | None:
    """Load a forecast plus its full update history."""
    with connect() as conn:
        f_row = conn.execute(
            "SELECT * FROM forecasts WHERE id = ?", (forecast_id,)
        ).fetchone()
        if f_row is None:
            return None
        u_rows = conn.execute(
            "SELECT * FROM forecast_updates WHERE forecast_id = ? ORDER BY created_at ASC",
            (forecast_id,),
        ).fetchall()
    return _row_to_forecast(f_row, u_rows)


def list_forecasts(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ForecastRecord]:
    """List forecasts with optional status filter.

    `status`:
        - "active": unresolved, not ambiguous
        - "resolved": has an outcome (not ambiguous)
        - "ambiguous": is_ambiguous = true
        - None: all
    """
    sql = "SELECT * FROM forecasts"
    params: list = []
    if status == "active":
        sql += " WHERE outcome IS NULL AND is_ambiguous = 0"
    elif status == "resolved":
        sql += " WHERE outcome IS NOT NULL AND is_ambiguous = 0"
    elif status == "ambiguous":
        sql += " WHERE is_ambiguous = 1"
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with connect() as conn:
        f_rows = conn.execute(sql, params).fetchall()
        if not f_rows:
            return []

        # One query for every forecast's updates, not one per forecast. At the default
        # limit that is 2 round-trips instead of 51.
        ids = [r["id"] for r in f_rows]
        placeholders = ",".join("?" * len(ids))
        u_rows = conn.execute(
            f"SELECT * FROM forecast_updates WHERE forecast_id IN ({placeholders}) "
            f"ORDER BY created_at ASC",
            ids,
        ).fetchall()

    by_forecast: dict[str, list] = {fid: [] for fid in ids}
    for u in u_rows:
        by_forecast[u["forecast_id"]].append(u)
    return [_row_to_forecast(f, by_forecast[f["id"]]) for f in f_rows]


def list_active_forecast_ids() -> list[str]:
    """Forecasts eligible for the daily refresh: unresolved, non-ambiguous, before resolution_date."""
    now = _utcnow()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM forecasts
            WHERE outcome IS NULL AND is_ambiguous = 0 AND resolution_date > ?
            """,
            (now,),
        ).fetchall()
    return [r["id"] for r in rows]


def compute_time_weighted_probability(forecast_id: str) -> float:
    """Read a forecast's update history and score it. Maths in `scoring`."""
    with connect() as conn:
        f_row = conn.execute(
            "SELECT resolution_date FROM forecasts WHERE id = ?", (forecast_id,)
        ).fetchone()
        if f_row is None:
            raise NotFoundError(f"forecast {forecast_id}")
        resolution_date = _ensure_aware(f_row["resolution_date"])
        u_rows = conn.execute(
            "SELECT probability, created_at FROM forecast_updates "
            "WHERE forecast_id = ? ORDER BY created_at ASC",
            (forecast_id,),
        ).fetchall()

    if not u_rows:
        raise StateError("forecast has no updates")

    return scoring.time_weighted_probability(
        [r["probability"] for r in u_rows],
        [_ensure_aware(r["created_at"]) for r in u_rows],
        resolution_date,
    )


def resolve_forecast(forecast_id: str, outcome: float | None) -> None:
    """Record a resolution.

    `outcome=None` marks the forecast as ambiguous (excluded from scoring).
    Otherwise computes scored_probability + brier_score from the update history.
    """
    now = _utcnow()
    with connect() as conn:
        f_row = conn.execute(
            "SELECT outcome, is_ambiguous FROM forecasts WHERE id = ?", (forecast_id,)
        ).fetchone()
        if f_row is None:
            raise NotFoundError(f"forecast {forecast_id}")
        if f_row["outcome"] is not None or f_row["is_ambiguous"]:
            raise StateError("forecast already resolved")

    if outcome is None:
        with connect() as conn:
            conn.execute(
                "UPDATE forecasts SET is_ambiguous = 1, resolved_at = ? WHERE id = ?",
                (now, forecast_id),
            )
        return

    if outcome not in (0.0, 1.0):
        raise ValueError("outcome must be 0.0, 1.0, or None")

    scored = compute_time_weighted_probability(forecast_id)
    brier = (scored - outcome) ** 2

    with connect() as conn:
        conn.execute(
            """
            UPDATE forecasts SET
                outcome = ?,
                resolved_at = ?,
                scored_probability = ?,
                brier_score = ?
            WHERE id = ?
            """,
            (outcome, now, scored, brier, forecast_id),
        )


def mark_refreshed(forecast_id: str, flagged: bool = False) -> None:
    """Update last_refreshed_at and optionally set flagged_for_resolution_review."""
    now = _utcnow()
    with connect() as conn:
        if flagged:
            conn.execute(
                "UPDATE forecasts SET last_refreshed_at = ?, flagged_for_resolution_review = 1 WHERE id = ?",
                (now, forecast_id),
            )
        else:
            conn.execute(
                "UPDATE forecasts SET last_refreshed_at = ? WHERE id = ?",
                (now, forecast_id),
            )


def calibration_report() -> CalibrationReport:
    """Read every resolved, non-ambiguous forecast and score it. Maths in `scoring`."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT scored_probability, outcome, brier_score
            FROM forecasts
            WHERE outcome IS NOT NULL AND is_ambiguous = 0 AND scored_probability IS NOT NULL
            """
        ).fetchall()
        ambiguous_count = conn.execute(
            "SELECT COUNT(*) FROM forecasts WHERE is_ambiguous = 1"
        ).fetchone()[0]

    return scoring.calibration(
        [(r["scored_probability"], r["outcome"], r["brier_score"]) for r in rows],
        ambiguous_count,
    )

# ---------- Gated runs ----------
#
# A run is a persisted state machine of user-gated stages (ADR 45). Every stage output
# lands here as a `run_steps` row, so the whole reasoning trail survives a restart and
# "retry" is re-running one step from what the database already knows. Decisions about
# *which* transitions are legal live in `machine`; this section only reads and writes.

STAGE_ORDER: tuple[str, ...] = (
    "decompose",
    "lenses",
    "base_rates",
    "inside_view",
    "synthesis",
)
"""The five gated stages, in the order the user advances through them."""


def create_gated_run(
    *,
    question: str = "",
    resolution_criteria: str = "",
    resolution_source: str = "",
    resolution_date: datetime | None = None,
    category: str = "general",
    max_iterations: int = 5,
) -> dict:
    """Insert a run in `backlog`. Partial fields are fine — the start gate checks them."""
    run_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """INSERT INTO gated_runs (id, question, resolution_criteria,
                                       resolution_source, resolution_date, category,
                                       max_iterations, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'backlog', ?)""",
            (
                run_id,
                question,
                resolution_criteria,
                resolution_source,
                resolution_date,
                category,
                max_iterations,
                _utcnow(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM gated_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _row_to_gated_run(row)


def update_gated_run_fields(
    run_id: str,
    *,
    question: str | None = None,
    resolution_criteria: str | None = None,
    resolution_source: str | None = None,
    resolution_date: datetime | None = None,
    category: str | None = None,
    max_iterations: int | None = None,
) -> dict:
    """Edit a backlog run's question fields. Raises StateError once it has started."""
    updates = {
        "question": question,
        "resolution_criteria": resolution_criteria,
        "resolution_source": resolution_source,
        "resolution_date": resolution_date,
        "category": category,
        "max_iterations": max_iterations,
    }
    sets = [f"{col} = ?" for col, v in updates.items() if v is not None]
    params: list = [v for v in updates.values() if v is not None]

    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM gated_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id}")
        if row["status"] != "backlog":
            raise StateError("can only edit a run while it is in the backlog")
        if sets:
            params.append(run_id)
            conn.execute(
                f"UPDATE gated_runs SET {', '.join(sets)} WHERE id = ?", params
            )
        fresh = conn.execute(
            "SELECT * FROM gated_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _row_to_gated_run(fresh)


def start_gated_run(run_id: str) -> None:
    """CAS `backlog` → `active`. Raises StateError if it already left the backlog."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE gated_runs SET status = 'active', started_at = ? "
            "WHERE id = ? AND status = 'backlog'",
            (_utcnow(), run_id),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT status FROM gated_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"run {run_id}")
            raise StateError(f"run is {row['status']}, not backlog")


def complete_gated_run(run_id: str, forecast_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE gated_runs SET status = 'complete', forecast_id = ?, "
            "completed_at = ?, error = NULL WHERE id = ?",
            (forecast_id, _utcnow(), run_id),
        )


def get_gated_run(run_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM gated_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _row_to_gated_run(row) if row is not None else None


def list_gated_runs(limit: int = 100) -> list[dict]:
    """Newest first, with per-stage step counts folded in for the sidebar."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM gated_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        counts = conn.execute(
            """SELECT run_id, stage, status, COUNT(*) AS n FROM run_steps
               GROUP BY run_id, stage, status"""
        ).fetchall()

    by_run: dict[str, dict[str, dict[str, int]]] = {}
    for c in counts:
        by_run.setdefault(c["run_id"], {}).setdefault(c["stage"], {})[c["status"]] = c[
            "n"
        ]
    out = []
    for r in rows:
        run = _row_to_gated_run(r)
        run["stage_counts"] = by_run.get(r["id"], {})
        out.append(run)
    return out


def delete_gated_run(run_id: str) -> None:
    """Remove a run and (via CASCADE) its steps. The saved forecast, if any, stays."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM gated_runs WHERE id = ?", (run_id,))
        if cur.rowcount == 0:
            raise NotFoundError(f"run {run_id}")


def insert_steps(
    run_id: str, steps: list[tuple[str, str, str]]
) -> list[dict]:
    """Materialize pending steps. Each entry is (stage, sub_claim_id, lens_name).

    `INSERT OR IGNORE` against the UNIQUE key makes re-materialization idempotent —
    advancing a stage twice (a retried final cell, say) must not duplicate rows.
    """
    with connect() as conn:
        for stage, sub_claim_id, lens_name in steps:
            conn.execute(
                """INSERT OR IGNORE INTO run_steps (id, run_id, stage, sub_claim_id, lens_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), run_id, stage, sub_claim_id, lens_name),
            )
        rows = conn.execute(
            "SELECT * FROM run_steps WHERE run_id = ?", (run_id,)
        ).fetchall()
    return [_row_to_step(r) for r in rows]


def delete_steps(step_ids: list[str]) -> int:
    """Remove step rows by id. Returns how many went. Empty list is a no-op.

    Only `machine.reconcile` calls this, and only for rows still `pending` — it checks
    that itself, because a delete here cannot be undone.
    """
    if not step_ids:
        return 0
    placeholders = ",".join("?" for _ in step_ids)
    with connect() as conn:
        cur = conn.execute(
            f"DELETE FROM run_steps WHERE id IN ({placeholders})", tuple(step_ids)
        )
    return cur.rowcount


def edit_step_payload(step_id: str, payload_json: str) -> dict:
    """Replace a completed step's payload with one a person wrote.

    `status` and `attempts` deliberately do not move: the step is still complete, and an
    edit is not an attempt. `edited_at` is what tells the two kinds of payload apart.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE run_steps SET payload_json = ?, edited_at = ? WHERE id = ?",
            (payload_json, _utcnow(), step_id),
        )
        row = conn.execute(
            "SELECT * FROM run_steps WHERE id = ?", (step_id,)
        ).fetchone()
    return _row_to_step(row)


def claim_step(step_id: str) -> dict | None:
    """CAS `pending`/`error` → `running`. Returns the claimed step, or None if lost.

    Also clears the owning run's red-chip error: a retry in flight is no longer a
    failed run until it fails again.
    """
    with connect() as conn:
        cur = conn.execute(
            """UPDATE run_steps
                  SET status = 'running', error = NULL, started_at = ?,
                      attempts = attempts + 1
                WHERE id = ? AND status IN ('pending', 'error')""",
            (_utcnow(), step_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM run_steps WHERE id = ?", (step_id,)
        ).fetchone()
        conn.execute(
            "UPDATE gated_runs SET error = NULL WHERE id = ?", (row["run_id"],)
        )
    return _row_to_step(row)


def finish_step(step_id: str, payload_json: str) -> dict:
    """Write a step's output and mark it complete."""
    with connect() as conn:
        conn.execute(
            """UPDATE run_steps
                  SET status = 'complete', payload_json = ?, error = NULL, finished_at = ?
                WHERE id = ?""",
            (payload_json, _utcnow(), step_id),
        )
        row = conn.execute(
            "SELECT * FROM run_steps WHERE id = ?", (step_id,)
        ).fetchone()
    return _row_to_step(row)


def fail_step(step_id: str, error: str) -> dict:
    """Mark a step failed and mirror the message onto the run's red chip."""
    with connect() as conn:
        conn.execute(
            """UPDATE run_steps
                  SET status = 'error', error = ?, finished_at = ?
                WHERE id = ?""",
            (error, _utcnow(), step_id),
        )
        row = conn.execute(
            "SELECT * FROM run_steps WHERE id = ?", (step_id,)
        ).fetchone()
        conn.execute(
            "UPDATE gated_runs SET error = ? WHERE id = ?", (error, row["run_id"])
        )
    return _row_to_step(row)


def get_step(step_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM run_steps WHERE id = ?", (step_id,)
        ).fetchone()
    return _row_to_step(row) if row is not None else None


def list_steps(run_id: str) -> list[dict]:
    """All of a run's steps, in stage order then creation order."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM run_steps WHERE run_id = ?", (run_id,)
        ).fetchall()
    order = {stage: i for i, stage in enumerate(STAGE_ORDER)}
    steps = [_row_to_step(r) for r in rows]
    steps.sort(key=lambda s: (order.get(s["stage"], 99), s["sub_claim_id"], s["lens_name"]))
    return steps


def mark_interrupted_steps() -> int:
    """Flip every still-running step to error. Called from `init_db`. Returns count."""
    message = "interrupted by restart"
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, run_id FROM run_steps WHERE status = 'running'"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE run_steps SET status = 'error', error = ?, finished_at = ? "
                "WHERE id = ?",
                (message, _utcnow(), row["id"]),
            )
            conn.execute(
                "UPDATE gated_runs SET error = ? WHERE id = ?",
                (message, row["run_id"]),
            )
    return len(rows)


def _row_to_gated_run(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "question": row["question"],
        "resolution_criteria": row["resolution_criteria"],
        "resolution_source": row["resolution_source"],
        "resolution_date": (
            _ensure_aware(row["resolution_date"]) if row["resolution_date"] else None
        ),
        "category": row["category"],
        "max_iterations": row["max_iterations"],
        "status": row["status"],
        "error": row["error"],
        "forecast_id": row["forecast_id"],
        "created_at": _ensure_aware(row["created_at"]),
        "started_at": _ensure_aware(row["started_at"]) if row["started_at"] else None,
        "completed_at": (
            _ensure_aware(row["completed_at"]) if row["completed_at"] else None
        ),
    }


def _row_to_step(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "stage": row["stage"],
        "sub_claim_id": row["sub_claim_id"],
        "lens_name": row["lens_name"],
        "status": row["status"],
        "payload_json": row["payload_json"],
        "error": row["error"],
        "attempts": row["attempts"],
        "started_at": _ensure_aware(row["started_at"]) if row["started_at"] else None,
        "finished_at": (
            _ensure_aware(row["finished_at"]) if row["finished_at"] else None
        ),
        "edited_at": _ensure_aware(row["edited_at"]) if row["edited_at"] else None,
    }


# ---------- Row → model converters ----------


def _row_to_forecast(f_row: sqlite3.Row, u_rows: list[sqlite3.Row]) -> ForecastRecord:
    decompositions = [
        SubPrediction(**d) for d in json.loads(f_row["decompositions_json"])
    ]
    research = ResearchSummary.model_validate_json(f_row["research_json"])
    updates = [
        ForecastUpdateRecord(
            id=u["id"],
            forecast_id=u["forecast_id"],
            probability=u["probability"],
            reasoning=u["reasoning"],
            is_late=bool(u["is_late"]),
            created_at=_ensure_aware(u["created_at"]),
        )
        for u in u_rows
    ]
    return ForecastRecord(
        id=f_row["id"],
        question=f_row["question"],
        resolution_criteria=f_row["resolution_criteria"],
        resolution_source=f_row["resolution_source"],
        category=f_row["category"],
        submission_gap_days=f_row["submission_gap_days"],
        submission_deadline=_ensure_aware(f_row["submission_deadline"]),
        resolution_date=_ensure_aware(f_row["resolution_date"]),
        resolved_at=(
            _ensure_aware(f_row["resolved_at"]) if f_row["resolved_at"] else None
        ),
        outcome=f_row["outcome"],
        is_ambiguous=bool(f_row["is_ambiguous"]),
        scored_probability=f_row["scored_probability"],
        brier_score=f_row["brier_score"],
        last_refreshed_at=(
            _ensure_aware(f_row["last_refreshed_at"])
            if f_row["last_refreshed_at"]
            else None
        ),
        flagged_for_resolution_review=bool(f_row["flagged_for_resolution_review"]),
        initial_reasoning=f_row["initial_reasoning"],
        decompositions=decompositions,
        research=research,
        updates=updates,
        created_at=_ensure_aware(f_row["created_at"]),
    )
