"""Scheduled jobs and orchestrators."""

from __future__ import annotations

import logfire
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_app_settings

from . import db
from superforecaster.models import RefreshSummary
from .update import run_update_graph


async def run_daily_refresh() -> RefreshSummary:
    """Run the update graph over every active forecast."""
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

    return summary


# ---------- Scheduler wiring ----------


_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    """Idempotent. Starts the scheduler with the refresh job registered."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    settings = get_app_settings()
    refresh_cron = settings.refresh_cron_schedule

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_daily_refresh,
        CronTrigger.from_crontab(refresh_cron, timezone="UTC"),
        id="daily_refresh",
        replace_existing=True,
    )
    scheduler.start()
    logfire.info("scheduler started", refresh=refresh_cron)
    print(f"Daily forecast refresh scheduler started (schedule: {refresh_cron})")
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
