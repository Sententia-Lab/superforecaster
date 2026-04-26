"""Forecast endpoints — public reads, admin-protected writes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from superforecaster import db
from superforecaster.agent import run_forecast
from superforecaster.models import (
    AddUpdateRequest,
    CreateForecastRequest,
    ForecastInput,
    ForecastRecord,
    ForecastUpdateRecord,
    RefreshActionResponse,
    ResolveRequest,
)
from superforecaster.refresh import refresh_forecast as do_refresh

from .deps import require_admin


router = APIRouter(prefix="/forecasts", tags=["forecasts"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_forecast(
    body: CreateForecastRequest, _: None = Depends(require_admin)
) -> ForecastRecord:
    """Run the forecast agent and persist the result. Admin only."""
    forecast = await run_forecast(
        ForecastInput(
            question=body.question,
            resolution_criteria=body.resolution_criteria,
            resolution_date=body.resolution_date,
            category=body.category,
        )
    )
    fid = db.save_forecast(
        forecast,
        resolution_source=body.resolution_source,
        submission_gap_days=body.submission_gap_days,
    )
    record = db.get_forecast(fid)
    assert record is not None
    return record


@router.get("")
def list_forecasts(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = 20,
    offset: int = 0,
) -> list[ForecastRecord]:
    return db.list_forecasts(status=status_filter, limit=limit, offset=offset)


@router.get("/{forecast_id}")
def get_forecast(forecast_id: str) -> ForecastRecord:
    record = db.get_forecast(forecast_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="forecast not found")
    return record


@router.post("/{forecast_id}/updates", status_code=status.HTTP_201_CREATED)
def add_update(
    forecast_id: str,
    body: AddUpdateRequest,
    _: None = Depends(require_admin),
) -> ForecastUpdateRecord:
    try:
        return db.add_forecast_update(
            forecast_id=forecast_id,
            probability=body.probability,
            confidence=body.confidence,
            reasoning=body.reasoning,
        )
    except db.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except db.StateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.patch("/{forecast_id}/resolve")
def resolve(
    forecast_id: str, body: ResolveRequest, _: None = Depends(require_admin)
) -> ForecastRecord:
    try:
        db.resolve_forecast(forecast_id, outcome=body.outcome)
    except db.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except db.StateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    record = db.get_forecast(forecast_id)
    assert record is not None
    return record


@router.post("/{forecast_id}/refresh")
async def refresh(
    forecast_id: str, _: None = Depends(require_admin)
) -> RefreshActionResponse:
    """Manually trigger one refresh cycle for a single forecast."""
    return await do_refresh(forecast_id)
