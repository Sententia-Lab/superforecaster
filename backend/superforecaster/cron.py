"""Scheduled jobs and orchestrators.

Two jobs registered with APScheduler in the FastAPI lifespan:
- run_daily_refresh: resolution sweep + probability sweep across active forecasts
- run_monthly_digest: auto-promote top-voted pending questions

The orchestrator functions are also exposed as plain async callables so
the API and CLI can trigger them on demand.
"""

from __future__ import annotations

import logfire
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import get_settings

from . import db
from .models import RefreshSummary, QuestionRecord
from .graphs import run_update_graph


# ---------- Daily refresh (Spec 5) ----------


async def run_daily_refresh() -> RefreshSummary:
    """Run the update graph over every active forecast.

    Previously two sweeps with a `flagged_ids` set carried between them. The graph
    now enforces that ordering internally — `CheckResolved` short-circuits to `End`
    on a resolved forecast, so the probability update is unreachable for it — which
    means this is a single loop and the invariant lives in one place.
    """
    summary = RefreshSummary()
    forecast_ids = db.list_active_forecast_ids()
    summary.total_checked = len(forecast_ids)

    for fid in forecast_ids:
        try:
            outcome = await run_update_graph(fid)
        except Exception as exc:  # noqa: BLE001
            summary.errors.append(f"update {fid}: {exc}")
            logfire.error(f"update graph error on {fid}: {exc}")
            continue

        if outcome.flagged_resolved:
            summary.total_flagged_for_review += 1
            summary.total_skipped += 1
        elif outcome.updated:
            summary.total_updated += 1
        else:
            summary.total_skipped += 1

    db.record_refresh_run(summary.model_dump_json())
    return summary


# ---------- Monthly digest (Spec 4) ----------


def preview_monthly_digest(n: int = 5) -> list[QuestionRecord]:
    """What the monthly digest would promote right now. No mutation."""
    return db.get_top_monthly(n=n)


def run_monthly_digest(n: int = 5) -> list[QuestionRecord]:
    """Auto-promote top N voted pending questions to approved status."""
    top = db.get_top_monthly(n=n)
    promoted: list[QuestionRecord] = []
    for q in top:
        if q.status == "pending":
            try:
                promoted_record = db.approve_question(q.id)
                promoted.append(promoted_record)
                logfire.info(
                    "monthly digest promoted question",
                    question_id=q.id,
                    text=q.text,
                    net_score=q.net_score,
                )
            except Exception as exc:  # noqa: BLE001
                logfire.error(f"digest promotion failed for {q.id}: {exc}")
    return promoted


# ---------- Scheduler wiring ----------


_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    """Idempotent. Starts the scheduler with both jobs registered."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    settings = get_settings()
    refresh_cron = settings.refresh_cron_schedule
    digest_cron = settings.digest_cron_schedule

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_daily_refresh,
        CronTrigger.from_crontab(refresh_cron, timezone="UTC"),
        id="daily_refresh",
        replace_existing=True,
    )
    # Run digest on the LAST day of the month at 09:00 UTC. APScheduler's
    # cron supports "last" via day='last', not via standard crontab. We
    # use a 28-31 range and check `is_last_day` inside the job for safety.
    scheduler.add_job(
        _digest_if_last_day,
        CronTrigger.from_crontab(digest_cron, timezone="UTC"),
        id="monthly_digest",
        replace_existing=True,
    )
    scheduler.start()
    logfire.info(
        "schedulers started",
        refresh=refresh_cron,
        digest=digest_cron,
    )
    print(f"Daily forecast refresh scheduler started (schedule: {refresh_cron})")
    print(f"Monthly digest scheduler started (schedule: {digest_cron})")
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None


async def _digest_if_last_day() -> None:
    """Run digest only on the actual last day of the month."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).day
    if tomorrow == 1:  # tomorrow is the 1st → today is the last day
        run_monthly_digest()
