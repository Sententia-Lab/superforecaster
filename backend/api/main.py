"""FastAPI app entry point.

- Initializes DB on startup
- Starts the APScheduler (digest + daily refresh) on startup
- Mounts all routers
- Health check at /healthz (used by Docker healthcheck)
- CORS open by default — public API
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import config  # noqa: F401 — loads backend/.env
from config import get_settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from superforecaster import cron, db

from .admin import router as admin_router
from .calibration import router as calibration_router
from .forecasts import router as forecasts_router
from .questions import router as questions_router
from .runs import router as runs_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    cron.start_scheduler()
    try:
        yield
    finally:
        cron.stop_scheduler()


app = FastAPI(
    title="Superforecaster API",
    description="Crowd-sourced forecasting platform powered by Tetlock's superforecasting methodology",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz", tags=["health"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(forecasts_router)
app.include_router(questions_router)
app.include_router(calibration_router)
app.include_router(admin_router)
app.include_router(runs_router)

# The static frontend. Mounted last and at "/" so it cannot shadow an API prefix —
# a mount at the root matches everything the routers above declined.
_frontend = Path(get_settings().frontend_dir)
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
