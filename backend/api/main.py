"""FastAPI app entry point.

- Initializes DB on startup
- Starts the APScheduler (daily refresh) on startup
- Mounts all routers
- Health check at /healthz (used by Docker healthcheck)
- CORS open by default — public API
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from app.config import get_app_settings, load_env, origin, set_runtime_key
from superforecaster.config import (
    active_llm_key_name,
    get_settings,
    resolve_agent_model,
)

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import cron, db

from .admin import router as admin_router
from .deps import is_local_mode, require_admin
from .calibration import router as calibration_router
from .forecasts import router as forecasts_router
from .questions import router as questions_router
from .runs import router as runs_router

# Before anything reads a setting. The core library no longer loads `.env` on import —
# it is a library — so the process that wants a `.env` says so, and this is that line.
load_env()


def _preflight() -> list[str]:
    """What this process is actually configured to do, as lines to print at startup.

    A forecast that quietly ran without web search looks exactly like one that had it —
    same shape, same confidence, worse answer. Same for a model nobody chose. Saying it
    once at startup costs four lines and turns "why is this bad" into something visible.
    """
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
    lines.append(
        "  admin auth    ADMIN_API_KEY"
        if app_s.admin_api_key
        else "  admin auth    local mode — unauthenticated requests from localhost only"
    )
    lines.append(f"  database      {app_s.database_path}")
    return lines


@asynccontextmanager
async def lifespan(app: FastAPI):
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
def client_config(request: Request) -> dict[str, object]:
    """What the frontend needs to know about this server before it renders.

    `auth_required` is the load-bearing one. The client used to decide for itself that a
    run needs a token and refuse before sending anything, so a local server with no
    `ADMIN_API_KEY` — the one-command case — answered "Admin token not set" to a button
    the server would have honoured. The server is the authority on its own auth; this is
    how the client asks.

    Public and deliberately thin: three booleans and a model name, nothing an
    unauthenticated caller could not learn by trying a request. `keys` says where each
    key came from and never what it is — see ADR 61.
    """
    return _client_config(request)


def _client_config(request: Request) -> dict[str, object]:
    s = get_settings()
    try:
        model = resolve_agent_model()
    except RuntimeError:
        model = ""
    return {
        "auth_required": not is_local_mode(request),
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
def set_keys(
    body: KeyUpdate, request: Request, _: None = Depends(require_admin)
) -> dict[str, object]:
    """Set API keys for the life of this process. ADR 61.

    Write-only: the response is the same `keys` origin map `GET /config` returns, so the
    panel redraws from it without a follow-up read, and no route ever hands a key back.
    """
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

    return _client_config(request)


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
