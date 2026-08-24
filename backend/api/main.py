"""FastAPI app entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from app.config import get_app_settings, load_env, origin, set_runtime_key
from app.observability import configure_logfire
from superforecaster.config import (
    active_llm_key_name,
    get_settings,
    resolve_agent_model,
)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import cron, db

from .admin import router as admin_router
from .calibration import router as calibration_router
from .forecasts import router as forecasts_router
from .questions import router as questions_router
from .runs import router as runs_router

# Before anything reads a setting. The core library no longer loads `.env` on import —
# it is a library — so the process that wants a `.env` says so, and this is that line.
load_env()


def _preflight() -> list[str]:
    """What this process is actually configured to do, as lines to print at startup."""
    s = get_settings()
    app_s = get_app_settings()
    lines = []

    try:
        lines.append(f"  model         {resolve_agent_model()}")
    except RuntimeError as e:
        lines.append(f"  model         NOT CONFIGURED — {e}")

    lines.append(
        "  web search    Tavily"
        if s.tavily_api_key
        else "  web search    OFF — set TAVILY_API_KEY. Wikipedia still works; base "
        "rates will be thinner."
    )
    lines.append(f"  database      {app_s.database_path}")
    return lines


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Core emits spans but never configures Logfire. This process decides where they go.
    configure_logfire()
    db.init_db()
    cron.start_scheduler()
    print("\nSuperforecaster", flush=True)
    for line in _preflight():
        print(line, flush=True)
    print("", flush=True)
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


@app.get("/config", tags=["health"])
def client_config() -> dict[str, object]:
    """What the frontend needs to know about this server before it renders."""
    return _client_config()


def _client_config() -> dict[str, object]:
    s = get_settings()
    try:
        model = resolve_agent_model()
    except RuntimeError:
        model = ""
    return {
        "search_enabled": bool(s.tavily_api_key),
        "model": model,
        "keys": {
            "llm": origin(active_llm_key_name()),
            # Which variable the LLM row is talking about. A gateway install and a direct
            # Anthropic install are credentialed by different names, and a panel that
            # named only one would lie on the other.
            "llm_var": active_llm_key_name(),
            "tavily": origin("TAVILY_API_KEY"),
            "wikipedia": origin("WIKIPEDIA_API_KEY"),
        },
    }


class KeyUpdate(BaseModel):
    """One field per settable key. `None` leaves it alone, `""` clears it."""

    llm_api_key: str | None = None
    tavily_api_key: str | None = None
    wikipedia_api_key: str | None = None


@app.put("/config/keys", tags=["health"])
def set_keys(body: KeyUpdate) -> dict[str, object]:
    """Set API keys for the life of this process. ADR 61."""
    for field, name in (
        # The LLM row writes whichever variable is credentialing the model, so setting a
        # key through the panel always changes the key the next run actually uses.
        ("llm_api_key", active_llm_key_name()),
        ("tavily_api_key", "TAVILY_API_KEY"),
        ("wikipedia_api_key", "WIKIPEDIA_API_KEY"),
    ):
        value = getattr(body, field)
        if value is None:
            continue
        try:
            set_runtime_key(name, value.strip())
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    return _client_config()


app.include_router(forecasts_router)
app.include_router(questions_router)
app.include_router(calibration_router)
app.include_router(admin_router)
app.include_router(runs_router)

# The static frontend. Mounted last and at "/" so it cannot shadow an API prefix —
# a mount at the root matches everything the routers above declined.
_frontend = Path(get_app_settings().frontend_dir)
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
