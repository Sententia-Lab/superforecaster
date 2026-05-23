"""Backend configuration — loads `backend/.env` and exposes typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_ROOT = Path(__file__).resolve().parent
load_dotenv(_BACKEND_ROOT / ".env", override=False)


@dataclass(frozen=True, slots=True)
class Settings:
    admin_api_key: str | None
    pydantic_ai_gateway_api_key: str | None
    tavily_api_key: str | None
    logfire_token: str | None
    database_path: str
    refresh_cron_schedule: str
    digest_cron_schedule: str
    min_probability_delta: float
    search_lookback_hours: int


def get_settings() -> Settings:
    return Settings(
        admin_api_key=os.getenv("ADMIN_API_KEY"),
        pydantic_ai_gateway_api_key=os.getenv("PYDANTIC_AI_GATEWAY_API_KEY"),
        tavily_api_key=os.getenv("TAVILY_API_KEY"),
        logfire_token=os.getenv("LOGFIRE_TOKEN"),
        database_path=os.getenv("DATABASE_PATH", "./superforecaster.db"),
        refresh_cron_schedule=os.getenv("REFRESH_CRON_SCHEDULE", "0 6 * * *"),
        digest_cron_schedule=os.getenv("DIGEST_CRON_SCHEDULE", "0 9 28-31 * *"),
        min_probability_delta=float(os.getenv("MIN_PROBABILITY_DELTA", "0.03")),
        search_lookback_hours=int(os.getenv("SEARCH_LOOKBACK_HOURS", "48")),
    )
