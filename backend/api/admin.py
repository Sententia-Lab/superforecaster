"""Admin-only endpoints — refresh trigger."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import cron
from superforecaster.models import RefreshSummary

from .deps import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/refresh/run")
async def refresh_run(_: None = Depends(require_admin)) -> RefreshSummary:
    """Manually trigger run_daily_refresh across all eligible forecasts."""
    return await cron.run_daily_refresh()
