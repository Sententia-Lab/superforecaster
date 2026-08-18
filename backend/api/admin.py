"""Admin-only endpoints — refresh trigger."""

from __future__ import annotations

from fastapi import APIRouter

from app import cron
from superforecaster.models import RefreshSummary

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/refresh/run")
async def refresh_run() -> RefreshSummary:
    """Manually trigger run_daily_refresh across all eligible forecasts."""
    return await cron.run_daily_refresh()
