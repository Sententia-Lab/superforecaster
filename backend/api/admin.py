"""Admin-only endpoints — digest preview/run, refresh trigger, refresh history."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from superforecaster import cron
from superforecaster.models import QuestionRecord, RefreshSummary

from .deps import require_admin


router = APIRouter(prefix="/admin", tags=["admin"])


class RefreshStatusResponse(BaseModel):
    last_run_started_at: datetime | None = None
    last_summary: RefreshSummary | None = None


@router.get("/digest/preview")
def digest_preview(_: None = Depends(require_admin)) -> list[QuestionRecord]:
    """What the monthly digest would promote right now. No mutation."""
    return cron.preview_monthly_digest(n=5)


@router.post("/digest/run")
def digest_run(_: None = Depends(require_admin)) -> list[QuestionRecord]:
    """Manually trigger the digest (auto-promote top 5 pending)."""
    return cron.run_monthly_digest(n=5)


@router.post("/refresh/run")
async def refresh_run(_: None = Depends(require_admin)) -> RefreshSummary:
    """Manually trigger run_daily_refresh across all eligible forecasts."""
    return await cron.run_daily_refresh()


@router.get("/refresh/status")
def refresh_status(_: None = Depends(require_admin)) -> RefreshStatusResponse:
    """Result of the most recent refresh run."""
    from superforecaster import db

    last = db.last_refresh_run()
    if last is None:
        return RefreshStatusResponse()
    return RefreshStatusResponse(
        last_run_started_at=last["started_at"],
        last_summary=RefreshSummary(**last["summary"]),
    )
