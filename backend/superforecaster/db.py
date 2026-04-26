"""SQLite persistence layer.

All DB operations live here. The schema is defined in `init_db()` and
created on first connection. Queries are intentionally written in plain
SQL — there is no ORM. The complexity is low enough that an ORM would
add overhead without value.

Scoring math (time-weighted Brier) is implemented here too because it
is fundamentally a query against the `forecast_updates` table.
"""

from __future__ import annotations

import hashlib
import json
import os
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


class PermissionError(Exception):  # noqa: A001 — shadowing builtin intentionally for namespacing
    """Raised when an IP tries to modify a question they didn't submit."""


class StateError(Exception):
    """Raised on an invalid state transition (e.g. updating after resolution)."""


# ---------- Connection ----------


def _db_path() -> str:
    return os.getenv("DATABASE_PATH", "./superforecaster.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection with sane defaults.

    Foreign keys are enforced. Row factory returns dicts for ergonomic access.
    """
    conn = sqlite3.connect(_db_path(), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Safe to call repeatedly."""
    with connect() as conn:
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
                confidence TEXT NOT NULL,
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
            """
        )


# ---------- Helpers ----------


def hash_ip(ip: str) -> str:
    """Hash a raw IP address for privacy-preserving storage."""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


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
                id, forecast_id, probability, confidence, reasoning, is_late, created_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                str(uuid.uuid4()),
                fid,
                forecast.probability,
                forecast.confidence,
                forecast.reasoning,
                now,
            ),
        )
    return fid


def add_forecast_update(
    forecast_id: str,
    probability: float,
    confidence: str,
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
                id, forecast_id, probability, confidence, reasoning, is_late, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (update_id, forecast_id, probability, confidence, reasoning, int(is_late), now),
        )

    return ForecastUpdateRecord(
        id=update_id,
        forecast_id=forecast_id,
        probability=probability,
        confidence=confidence,  # type: ignore[arg-type]
        reasoning=reasoning,
        is_late=is_late,
        created_at=now,
    )


def get_forecast(forecast_id: str) -> ForecastRecord | None:
    """Load a forecast plus its full update history."""
    with connect() as conn:
        f_row = conn.execute("SELECT * FROM forecasts WHERE id = ?", (forecast_id,)).fetchone()
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
        results = []
        for f_row in f_rows:
            u_rows = conn.execute(
                "SELECT * FROM forecast_updates WHERE forecast_id = ? ORDER BY created_at ASC",
                (f_row["id"],),
            ).fetchall()
            results.append(_row_to_forecast(f_row, u_rows))
    return results


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
    """Time-weighted average of all updates, weighted by held duration.

    Each update is held from its `created_at` until the next update's
    `created_at` (or `resolution_date` for the final update). The weight
    is its held duration as a fraction of the total horizon.
    """
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

    timestamps = [_ensure_aware(r["created_at"]) for r in u_rows]
    probabilities = [r["probability"] for r in u_rows]
    boundaries = timestamps + [resolution_date]
    total_seconds = (resolution_date - timestamps[0]).total_seconds()

    if total_seconds <= 0:
        # Degenerate case: only the latest probability matters
        return probabilities[-1]

    weighted_sum = 0.0
    for i, prob in enumerate(probabilities):
        duration = (boundaries[i + 1] - boundaries[i]).total_seconds()
        weight = duration / total_seconds
        weighted_sum += prob * weight
    return weighted_sum


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
    """Aggregate Brier score + per-bucket calibration over resolved, non-ambiguous forecasts."""
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

    if not rows:
        return CalibrationReport(
            aggregate_brier_score=None,
            total_resolved=0,
            total_ambiguous_excluded=ambiguous_count,
            buckets=[],
        )

    aggregate = sum(r["brier_score"] for r in rows) / len(rows)

    # 10 deciles
    buckets: list[CalibrationBucket] = []
    for i in range(10):
        low = i / 10
        high = (i + 1) / 10
        bucket_rows = [
            r for r in rows
            if (low <= r["scored_probability"] < high)
            or (i == 9 and r["scored_probability"] == 1.0)
        ]
        if not bucket_rows:
            continue
        buckets.append(
            CalibrationBucket(
                range=f"{int(low * 100)}-{int(high * 100)}%",
                predicted_avg=sum(r["scored_probability"] for r in bucket_rows) / len(bucket_rows),
                actual_frequency=sum(r["outcome"] for r in bucket_rows) / len(bucket_rows),
                count=len(bucket_rows),
            )
        )

    return CalibrationReport(
        aggregate_brier_score=aggregate,
        total_resolved=len(rows),
        total_ambiguous_excluded=ambiguous_count,
        buckets=buckets,
    )


# ---------- Questions ----------


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

    record = get_question(qid, requester_ip_hash=ip_hash)
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
    if text is None and resolution_criteria is None and proposed_resolution_date is None:
        raise ValueError("at least one field must be provided")

    now = _utcnow()
    with connect() as conn:
        row = conn.execute(
            "SELECT ip_hash, status, is_deleted FROM questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if row is None or row["is_deleted"]:
            raise NotFoundError(f"question {question_id}")

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

    record = get_question(question_id, requester_ip_hash=ip_hash)
    assert record is not None
    return record


def delete_question(question_id: str, ip_hash: str, is_admin: bool = False) -> None:
    """Soft-delete a question. Only the original submitter (or admin) can delete."""
    with connect() as conn:
        row = conn.execute(
            "SELECT ip_hash, status, is_deleted FROM questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if row is None or row["is_deleted"]:
            raise NotFoundError(f"question {question_id}")
        if not is_admin:
            if ip_hash != row["ip_hash"]:
                raise PermissionError("only the original submitter can delete")
            if row["status"] != "pending":
                raise StateError("can only delete pending questions")

        conn.execute("UPDATE questions SET is_deleted = 1 WHERE id = ?", (question_id,))


def get_question(question_id: str, requester_ip_hash: str | None = None) -> QuestionRecord | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM questions WHERE id = ? AND is_deleted = 0",
            (question_id,),
        ).fetchone()
        if row is None:
            return None
        net_score_row = conn.execute(
            "SELECT COALESCE(SUM(vote), 0) AS net FROM votes WHERE question_id = ?",
            (question_id,),
        ).fetchone()
        net_score = net_score_row["net"] if net_score_row else 0
        user_vote = None
        if requester_ip_hash is not None:
            v_row = conn.execute(
                "SELECT vote FROM votes WHERE question_id = ? AND ip_hash = ?",
                (question_id, requester_ip_hash),
            ).fetchone()
            user_vote = v_row["vote"] if v_row else None

    is_own = requester_ip_hash is not None and row["ip_hash"] == requester_ip_hash
    return _row_to_question(row, net_score, user_vote, is_own)


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

    record = get_question(question_id)
    assert record is not None
    return record


def reject_question(question_id: str) -> QuestionRecord:
    with connect() as conn:
        row = conn.execute(
            "SELECT status, is_deleted FROM questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if row is None or row["is_deleted"]:
            raise NotFoundError(f"question {question_id}")
        if row["status"] not in ("pending", "approved"):
            raise StateError(f"cannot reject question with status {row['status']}")
        conn.execute("UPDATE questions SET status = 'rejected' WHERE id = ?", (question_id,))
    record = get_question(question_id)
    assert record is not None
    return record


def link_question_to_forecast(question_id: str, forecast_id: str) -> QuestionRecord:
    """Admin: mark a question as forecasted and link to its forecast row."""
    with connect() as conn:
        row = conn.execute(
            "SELECT status, is_deleted FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        if row is None or row["is_deleted"]:
            raise NotFoundError(f"question {question_id}")
        if row["status"] != "approved":
            raise StateError(f"can only forecast approved questions, got {row['status']}")
        conn.execute(
            "UPDATE questions SET status = 'forecasted', forecast_id = ? WHERE id = ?",
            (forecast_id, question_id),
        )
    record = get_question(question_id)
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

        existing = conn.execute(
            "SELECT id FROM votes WHERE question_id = ? AND ip_hash = ?",
            (question_id, ip_hash),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO votes (id, question_id, ip_hash, vote, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), question_id, ip_hash, vote, now, now),
            )
        else:
            conn.execute(
                "UPDATE votes SET vote = ?, updated_at = ? WHERE id = ?",
                (vote, now, existing["id"]),
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
    return {"started_at": _ensure_aware(row["started_at"]), "summary": json.loads(row["summary_json"])}


# ---------- Row → model converters ----------


def _row_to_forecast(f_row: sqlite3.Row, u_rows: list[sqlite3.Row]) -> ForecastRecord:
    decompositions = [SubPrediction(**d) for d in json.loads(f_row["decompositions_json"])]
    research = ResearchSummary.model_validate_json(f_row["research_json"])
    updates = [
        ForecastUpdateRecord(
            id=u["id"],
            forecast_id=u["forecast_id"],
            probability=u["probability"],
            confidence=u["confidence"],
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
        resolved_at=_ensure_aware(f_row["resolved_at"]) if f_row["resolved_at"] else None,
        outcome=f_row["outcome"],
        is_ambiguous=bool(f_row["is_ambiguous"]),
        scored_probability=f_row["scored_probability"],
        brier_score=f_row["brier_score"],
        last_refreshed_at=_ensure_aware(f_row["last_refreshed_at"]) if f_row["last_refreshed_at"] else None,
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
