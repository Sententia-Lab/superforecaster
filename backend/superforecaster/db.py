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

import hashlib
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
    QuestionRecord,
    QuestionStatus,
    ResearchSummary,
    SubPrediction,
)


# ---------- Errors ----------


class RateLimitError(Exception):
    """Raised when an IP attempts to submit more than once per 24h."""


class NotFoundError(Exception):
    """Raised when a record lookup returns no rows."""


class PermissionError(
    Exception
):  # noqa: A001 — shadowing builtin intentionally for namespacing
    """Raised when an IP tries to modify a question they didn't submit."""


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


SCHEMA_VERSION = 1
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
}
"""version -> statements that take the schema from `version - 1` to `version`.

Each step must be safe to apply to a database that reached the previous version by *either*
route: an old database being upgraded, or a fresh one just built by `init_db`'s
`CREATE TABLE` block. `_migrate` skips a step whose work the fresh schema already did, so
"drop a column that no longer exists" is a no-op rather than an error.
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
                # The fresh-schema case: the column this step drops was never created.
                # Anything else is a real failure and has to surface.
                if "no such column" not in str(e).lower():
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

            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                resolution_criteria TEXT NOT NULL,
                proposed_resolution_date TIMESTAMP NOT NULL,
                ip_hash TEXT NOT NULL,
                edited_at TIMESTAMP,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL,
                approved_at TIMESTAMP,
                forecast_id TEXT REFERENCES forecasts(id)
            );

            CREATE INDEX IF NOT EXISTS ix_questions_status ON questions(status);
            CREATE INDEX IF NOT EXISTS ix_questions_ip_created ON questions(ip_hash, created_at);

            CREATE TABLE IF NOT EXISTS votes (
                id TEXT PRIMARY KEY,
                question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                ip_hash TEXT NOT NULL,
                vote INTEGER NOT NULL CHECK(vote IN (-1, 1)),
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                UNIQUE(question_id, ip_hash)
            );

            CREATE INDEX IF NOT EXISTS ix_votes_question ON votes(question_id);

            CREATE TABLE IF NOT EXISTS refresh_runs (
                id TEXT PRIMARY KEY,
                started_at TIMESTAMP NOT NULL,
                summary_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                resolution_criteria TEXT NOT NULL,
                resolution_source TEXT NOT NULL DEFAULT '',
                resolution_date TIMESTAMP NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                forecast_id TEXT REFERENCES forecasts(id) ON DELETE SET NULL,
                error TEXT,
                created_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);
            CREATE INDEX IF NOT EXISTS ix_runs_created ON runs(created_at DESC);
            """
        )
        if fresh:
            # Born at the current schema, so no historical step has anything to do here.
            # Stamping now is what stops a future migration — a rename, say — from
            # "upgrading" a database that was already correct.
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _migrate(conn)

    # A run only ever lives in memory (see `superforecaster.runs`). Anything still
    # marked live after a restart is gone, and saying so beats leaving the UI on a
    # spinner that will never resolve.
    mark_orphaned_runs_lost()


# ---------- Helpers ----------


def hash_ip(ip: str) -> str:
    """Hash a raw IP address for privacy-preserving storage."""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def _guard_question(conn: sqlite3.Connection, question_id: str):
    """Fetch a question for mutation, or raise. The precondition every writer shares.

    Written out four times before this, which is three chances for one copy to drift from
    the others on what "deleted" means.
    """
    row = conn.execute(
        "SELECT ip_hash, status, is_deleted FROM questions WHERE id = ?",
        (question_id,),
    ).fetchone()
    if row is None or row["is_deleted"]:
        raise NotFoundError(f"question {question_id}")
    return row


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

def submit_question(
    text: str,
    resolution_criteria: str,
    proposed_resolution_date: datetime,
    ip_hash: str,
) -> QuestionRecord:
    """Insert a new community question idea. Rate-limited to 1 per IP per 24h."""
    now = _utcnow()
    cutoff = now - timedelta(hours=24)
    with connect() as conn:
        recent = conn.execute(
            """
            SELECT 1 FROM questions
            WHERE ip_hash = ? AND is_deleted = 0 AND created_at > ?
            LIMIT 1
            """,
            (ip_hash, cutoff),
        ).fetchone()
        if recent is not None:
            raise RateLimitError("submit limit: 1 per IP per 24 hours")

        qid = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO questions (
                id, text, resolution_criteria, proposed_resolution_date,
                ip_hash, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (qid, text, resolution_criteria, proposed_resolution_date, ip_hash, now),
        )

        record = _read_question(conn, qid, requester_ip_hash=ip_hash)

    assert record is not None
    return record


def edit_question(
    question_id: str,
    ip_hash: str | None,
    text: str | None = None,
    resolution_criteria: str | None = None,
    proposed_resolution_date: datetime | None = None,
    is_admin: bool = False,
) -> QuestionRecord:
    """Edit a question. If `is_admin=True`, IP and status checks are skipped."""
    if (
        text is None
        and resolution_criteria is None
        and proposed_resolution_date is None
    ):
        raise ValueError("at least one field must be provided")

    now = _utcnow()
    with connect() as conn:
        row = _guard_question(conn, question_id)

        if not is_admin:
            if ip_hash != row["ip_hash"]:
                raise PermissionError("only the original submitter can edit")
            if row["status"] != "pending":
                raise StateError("can only edit pending questions")

        sets = []
        params: list = []
        if text is not None:
            sets.append("text = ?")
            params.append(text)
        if resolution_criteria is not None:
            sets.append("resolution_criteria = ?")
            params.append(resolution_criteria)
        if proposed_resolution_date is not None:
            sets.append("proposed_resolution_date = ?")
            params.append(proposed_resolution_date)
        sets.append("edited_at = ?")
        params.append(now)
        params.append(question_id)

        conn.execute(f"UPDATE questions SET {', '.join(sets)} WHERE id = ?", params)

        record = _read_question(conn, question_id, requester_ip_hash=ip_hash)

    assert record is not None
    return record


def delete_question(question_id: str, ip_hash: str, is_admin: bool = False) -> None:
    """Soft-delete a question. Only the original submitter (or admin) can delete."""
    with connect() as conn:
        row = _guard_question(conn, question_id)
        if not is_admin:
            if ip_hash != row["ip_hash"]:
                raise PermissionError("only the original submitter can delete")
            if row["status"] != "pending":
                raise StateError("can only delete pending questions")

        conn.execute("UPDATE questions SET is_deleted = 1 WHERE id = ?", (question_id,))


def _read_question(
    conn: sqlite3.Connection,
    question_id: str,
    requester_ip_hash: str | None = None,
) -> QuestionRecord | None:
    """Assemble a question on an existing connection.

    Split out so a mutation can return the row it just wrote *inside its own
    transaction*. Every writer used to close its connection and then call
    `get_question`, which opened a second one — two round-trips with a window between
    them where another writer could land, and the caller would be handed a record that
    was never the result of its own write.
    """
    row = conn.execute(
        "SELECT * FROM questions WHERE id = ? AND is_deleted = 0", (question_id,)
    ).fetchone()
    if row is None:
        return None

    net_row = conn.execute(
        "SELECT COALESCE(SUM(vote), 0) AS net FROM votes WHERE question_id = ?",
        (question_id,),
    ).fetchone()
    user_vote = None
    if requester_ip_hash is not None:
        v_row = conn.execute(
            "SELECT vote FROM votes WHERE question_id = ? AND ip_hash = ?",
            (question_id, requester_ip_hash),
        ).fetchone()
        user_vote = v_row["vote"] if v_row else None

    is_own = requester_ip_hash is not None and row["ip_hash"] == requester_ip_hash
    return _row_to_question(row, net_row["net"] if net_row else 0, user_vote, is_own)


def get_question(
    question_id: str, requester_ip_hash: str | None = None
) -> QuestionRecord | None:
    with connect() as conn:
        return _read_question(conn, question_id, requester_ip_hash)


def list_questions(
    status: QuestionStatus | None = None,
    sort: str = "score",
    limit: int = 50,
    offset: int = 0,
    requester_ip_hash: str | None = None,
) -> list[QuestionRecord]:
    """List non-deleted questions, sorted by net score or created_at."""
    sql = """
        SELECT q.*,
               COALESCE((SELECT SUM(vote) FROM votes v WHERE v.question_id = q.id), 0) AS net_score,
               (SELECT vote FROM votes v WHERE v.question_id = q.id AND v.ip_hash = ?) AS user_vote
        FROM questions q
        WHERE q.is_deleted = 0
    """
    params: list = [requester_ip_hash]
    if status is not None:
        sql += " AND q.status = ?"
        params.append(status)
    if sort == "score":
        sql += " ORDER BY net_score DESC, q.created_at DESC"
    else:
        sql += " ORDER BY q.created_at DESC"
    sql += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        _row_to_question(
            r,
            r["net_score"],
            r["user_vote"],
            requester_ip_hash is not None and r["ip_hash"] == requester_ip_hash,
        )
        for r in rows
    ]


def get_top_monthly(n: int = 5) -> list[QuestionRecord]:
    """Top N questions by net score among pending/approved this calendar month."""
    now = _utcnow()
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    sql = """
        SELECT q.*,
               COALESCE((SELECT SUM(vote) FROM votes v WHERE v.question_id = q.id), 0) AS net_score
        FROM questions q
        WHERE q.is_deleted = 0
          AND q.status IN ('pending', 'approved')
          AND q.created_at >= ?
        ORDER BY net_score DESC, q.created_at DESC
        LIMIT ?
    """
    with connect() as conn:
        rows = conn.execute(sql, (month_start, n)).fetchall()
    return [_row_to_question(r, r["net_score"], None, is_own=False) for r in rows]


def approve_question(
    question_id: str,
    resolution_date: datetime | None = None,
    resolution_criteria: str | None = None,
) -> QuestionRecord:
    """Admin: approve a pending question, optionally overriding date/criteria."""
    now = _utcnow()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM questions WHERE id = ? AND is_deleted = 0",
            (question_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"question {question_id}")
        if row["status"] != "pending":
            raise StateError(f"cannot approve question with status {row['status']}")

        sets = ["status = 'approved'", "approved_at = ?"]
        params: list = [now]
        if resolution_date is not None:
            sets.append("proposed_resolution_date = ?")
            params.append(resolution_date)
        if resolution_criteria is not None:
            sets.append("resolution_criteria = ?")
            params.append(resolution_criteria)
        params.append(question_id)
        conn.execute(f"UPDATE questions SET {', '.join(sets)} WHERE id = ?", params)

        record = _read_question(conn, question_id)

    assert record is not None
    return record


def reject_question(question_id: str) -> QuestionRecord:
    with connect() as conn:
        row = _guard_question(conn, question_id)
        if row["status"] not in ("pending", "approved"):
            raise StateError(f"cannot reject question with status {row['status']}")
        conn.execute(
            "UPDATE questions SET status = 'rejected' WHERE id = ?", (question_id,)
        )
        record = _read_question(conn, question_id)

    assert record is not None
    return record


def link_question_to_forecast(question_id: str, forecast_id: str) -> QuestionRecord:
    """Admin: mark a question as forecasted and link to its forecast row."""
    with connect() as conn:
        row = _guard_question(conn, question_id)
        if row["status"] != "approved":
            raise StateError(
                f"can only forecast approved questions, got {row['status']}"
            )
        conn.execute(
            "UPDATE questions SET status = 'forecasted', forecast_id = ? WHERE id = ?",
            (forecast_id, question_id),
        )
        record = _read_question(conn, question_id)

    assert record is not None
    return record


# ---------- Votes ----------


def cast_vote(question_id: str, ip_hash: str, vote: int) -> int:
    """Upsert a vote. Returns new net score."""
    if vote not in (-1, 1):
        raise ValueError("vote must be +1 or -1")
    now = _utcnow()
    with connect() as conn:
        # Verify question exists and isn't deleted
        q = conn.execute(
            "SELECT 1 FROM questions WHERE id = ? AND is_deleted = 0", (question_id,)
        ).fetchone()
        if q is None:
            raise NotFoundError(f"question {question_id}")

        # `votes` already carries UNIQUE(question_id, ip_hash), so the database can do
        # this atomically. The select-then-insert-or-update it replaces had a window
        # between the check and the write where a second vote from the same caller
        # raised an integrity error instead of updating.
        conn.execute(
            """
            INSERT INTO votes (id, question_id, ip_hash, vote, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(question_id, ip_hash) DO UPDATE SET
                vote = excluded.vote,
                updated_at = excluded.updated_at
            """,
            (str(uuid.uuid4()), question_id, ip_hash, vote, now, now),
        )
        net_row = conn.execute(
            "SELECT COALESCE(SUM(vote), 0) AS net FROM votes WHERE question_id = ?",
            (question_id,),
        ).fetchone()
    return net_row["net"]


def remove_vote(question_id: str, ip_hash: str) -> int:
    """Delete a caller's vote. Returns new net score."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM votes WHERE question_id = ? AND ip_hash = ?",
            (question_id, ip_hash),
        )
        net_row = conn.execute(
            "SELECT COALESCE(SUM(vote), 0) AS net FROM votes WHERE question_id = ?",
            (question_id,),
        ).fetchone()
    return net_row["net"]


def get_vote(question_id: str, ip_hash: str) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT vote FROM votes WHERE question_id = ? AND ip_hash = ?",
            (question_id, ip_hash),
        ).fetchone()
    return row["vote"] if row else None


# ---------- Refresh-run history ----------


def record_refresh_run(summary_json: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO refresh_runs (id, started_at, summary_json) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), _utcnow(), summary_json),
        )


def last_refresh_run() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT started_at, summary_json FROM refresh_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return {
        "started_at": _ensure_aware(row["started_at"]),
        "summary": json.loads(row["summary_json"]),
    }


# ---------- Live runs ----------
#
# Only the run's identity and terminal state are stored. The reasoning trail is not:
# it lives in the in-memory ring buffer in `superforecaster.runs` for as long as the
# run is watchable, and is then dropped. Persisting it would mean an event table and a
# replay path for something that is cheaper to re-run than to store.


def create_run(
    run_id: str,
    question: str,
    resolution_criteria: str,
    resolution_source: str,
    resolution_date: datetime,
    category: str,
) -> None:
    """Insert a queued run.

    Written before the background task is scheduled, so a crash in the gap between the
    two surfaces as a `lost` run rather than as no record at all.
    """
    with connect() as conn:
        conn.execute(
            """INSERT INTO runs (id, question, resolution_criteria, resolution_source,
                                 resolution_date, category, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)""",
            (
                run_id,
                question,
                resolution_criteria,
                resolution_source,
                resolution_date,
                category,
                _utcnow(),
            ),
        )


def finish_run(
    run_id: str,
    *,
    status: str,
    forecast_id: str | None = None,
    error: str | None = None,
) -> None:
    """Write a run's terminal state. Idempotent — safe to call from a `finally`."""
    with connect() as conn:
        conn.execute(
            """UPDATE runs
                  SET status = ?, forecast_id = ?, error = ?, ended_at = ?
                WHERE id = ?""",
            (status, forecast_id, error, _utcnow(), run_id),
        )


def get_run(run_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row is not None else None


def list_runs(status: str | None = None, limit: int = 20) -> list[dict]:
    """Newest first.

    Unlike the in-memory registry this survives a restart, which is what lets the UI
    show a `lost` run instead of nothing.
    """
    sql = "SELECT * FROM runs"
    params: list[object] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_run(r) for r in rows]


def mark_orphaned_runs_lost() -> int:
    """Flip every still-live run to `lost`. Called from `init_db`. Returns the count."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = 'lost', ended_at = ? "
            "WHERE status IN ('queued', 'running')",
            (_utcnow(),),
        )
        return cur.rowcount


def _row_to_run(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "question": row["question"],
        "resolution_criteria": row["resolution_criteria"],
        "resolution_source": row["resolution_source"],
        "resolution_date": _ensure_aware(row["resolution_date"]),
        "category": row["category"],
        "status": row["status"],
        "forecast_id": row["forecast_id"],
        "error": row["error"],
        "created_at": _ensure_aware(row["created_at"]),
        "ended_at": _ensure_aware(row["ended_at"]) if row["ended_at"] else None,
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


def _row_to_question(
    row: sqlite3.Row,
    net_score: int,
    user_vote: int | None,
    is_own: bool = False,
) -> QuestionRecord:
    return QuestionRecord(
        id=row["id"],
        text=row["text"],
        resolution_criteria=row["resolution_criteria"],
        proposed_resolution_date=_ensure_aware(row["proposed_resolution_date"]),
        net_score=net_score or 0,
        user_vote=user_vote,
        is_own=is_own,
        status=row["status"],
        edited_at=_ensure_aware(row["edited_at"]) if row["edited_at"] else None,
        is_deleted=bool(row["is_deleted"]),
        created_at=_ensure_aware(row["created_at"]),
        approved_at=_ensure_aware(row["approved_at"]) if row["approved_at"] else None,
        forecast_id=row["forecast_id"],
    )
