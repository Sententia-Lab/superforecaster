"""Forecast endpoints — public reads, admin-protected writes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app import db, research
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

router = APIRouter(prefix="/forecasts", tags=["forecasts"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_forecast(body: CreateForecastRequest) -> ForecastRecord:
    """Run the whole pipeline back-to-back (no gates) and persist.

    The gated flow (`/runs`) is the primary path; this blocking endpoint is the API
    twin of `superforecaster forecast` for scripted use. Runs live, with no
    `model` clamp — those exist for backtesting.
    """
    store = research.new_store()
    forecast, _violations = await run_all(
        ForecastInput(
            question=body.question,
            resolution_criteria=body.resolution_criteria,
            resolution_date=body.resolution_date,
            category=body.category,
        ),
        store=store,
    )
    fid = db.save_forecast(
        forecast,
        resolution_source=body.resolution_source,
        submission_gap_days=body.submission_gap_days,
        research_id=store.research_id,
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


@router.delete("/{forecast_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_forecast(forecast_id: str) -> None:
    """Remove a forecast, its updates, and the research store the run built for it.

    A gated run that produced this forecast survives with its `forecast_id` cleared —
    the mirror of `DELETE /runs/{id}`, which leaves the forecast alive.
    """
    try:
        db.delete_forecast(forecast_id)
    except db.NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


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
def resolve(forecast_id: str, body: ResolveRequest) -> ForecastRecord:
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
async def refresh(forecast_id: str) -> RefreshActionResponse:
    """Manually trigger one update cycle for a single forecast.

    Same graph the cron job runs — resolution check first, then the probability
    update. The response shape predates the graph and is kept for the frontend.
    """
    outcome = await run_update_graph(forecast_id)
    return RefreshActionResponse(updated=outcome.updated, reason=outcome.reason)
