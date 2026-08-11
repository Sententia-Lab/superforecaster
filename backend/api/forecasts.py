"""Forecast endpoints — public reads, admin-protected writes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app import db
from app.update import run_update_graph
from superforecaster.stages import run_all
from superforecaster.models import (
    AddUpdateRequest,
    CreateForecastRequest,
    ForecastInput,
    ForecastRecord,
    ForecastUpdateRecord,
    RefreshActionResponse,
    ResolveRequest,
)

from .deps import require_admin

router = APIRouter(prefix="/forecasts", tags=["forecasts"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_forecast(
    body: CreateForecastRequest, _: None = Depends(require_admin)
) -> ForecastRecord:
    """Run the whole pipeline back-to-back (no gates) and persist. Admin only.

    The gated flow (`/runs`) is the primary path; this blocking endpoint is the API
    twin of `superforecaster forecast` for scripted use. Runs live: no `as_of` or
    `model` clamp — those exist for backtesting.
    """
    forecast, _violations = await run_all(
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="forecast not found"
        )
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
    """Manually trigger one update cycle for a single forecast.

    Same graph the cron job runs — resolution check first, then the probability
    update. The response shape predates the graph and is kept for the frontend.
    """
    outcome = await run_update_graph(forecast_id)
    return RefreshActionResponse(updated=outcome.updated, reason=outcome.reason)
