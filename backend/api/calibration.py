"""Calibration / accuracy stats endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app import db
from superforecaster.models import CalibrationReport

router = APIRouter(tags=["calibration"])


@router.get("/calibration")
def calibration() -> CalibrationReport:
    return db.calibration_report()
