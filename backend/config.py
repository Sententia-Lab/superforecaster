"""Backend configuration — loads `backend/.env` and exposes typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import UsageLimits

_BACKEND_ROOT = Path(__file__).resolve().parent
load_dotenv(_BACKEND_ROOT / ".env", override=False)

DEFAULT_GATEWAY_MODEL = "gateway/anthropic:claude-sonnet-4-6"
DEFAULT_ANTHROPIC_MODEL = "anthropic:claude-sonnet-4-6"
GATEWAY_MIGRATION_URL = "https://pydantic.dev/docs/logfire/gateway-migration/"
DEFAULT_AGENT_REQUEST_LIMIT = 40
DEFAULT_AGENT_TOOL_CALLS_LIMIT = 20


@dataclass(frozen=True, slots=True)
class Settings:
    admin_api_key: str | None
    pydantic_ai_gateway_api_key: str | None
    anthropic_api_key: str | None
    tavily_api_key: str | None
    logfire_token: str | None
    agent_model: str | None
    database_path: str
    refresh_cron_schedule: str
    digest_cron_schedule: str 
    min_probability_delta: float
    search_lookback_hours: int
    agent_request_limit: int | None
    agent_tool_calls_limit: int | None


def _parse_optional_int(raw: str | None, *, unlimited_values: frozenset[str] = frozenset({"none", "unlimited"})) -> int | None:
    if raw is None:
        return None
    if raw.lower() in unlimited_values:
        return None
    return int(raw)


def get_settings() -> Settings:
    request_limit = _parse_optional_int(os.getenv("AGENT_REQUEST_LIMIT", str(DEFAULT_AGENT_REQUEST_LIMIT)))
    tool_calls_limit = _parse_optional_int(os.getenv("AGENT_TOOL_CALLS_LIMIT", str(DEFAULT_AGENT_TOOL_CALLS_LIMIT)))

    return Settings(
        admin_api_key=os.getenv("ADMIN_API_KEY"),
        pydantic_ai_gateway_api_key=os.getenv("PYDANTIC_AI_GATEWAY_API_KEY"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        tavily_api_key=os.getenv("TAVILY_API_KEY"),
        logfire_token=os.getenv("LOGFIRE_TOKEN"),
        agent_model=os.getenv("AGENT_MODEL"),
        database_path=os.getenv("DATABASE_PATH", "./superforecaster.db"),
        refresh_cron_schedule=os.getenv("REFRESH_CRON_SCHEDULE", "0 6 * * *"),
        digest_cron_schedule=os.getenv("DIGEST_CRON_SCHEDULE", "0 9 28-31 * *"),
        min_probability_delta=float(os.getenv("MIN_PROBABILITY_DELTA", "0.03")),
        search_lookback_hours=int(os.getenv("SEARCH_LOOKBACK_HOURS", "48")),
        agent_request_limit=request_limit,
        agent_tool_calls_limit=tool_calls_limit,
    )


def get_usage_limits(*, max_iterations: int | None = None) -> UsageLimits:
    settings = get_settings()
    request_limit = settings.agent_request_limit or DEFAULT_AGENT_REQUEST_LIMIT
    tool_calls_limit = settings.agent_tool_calls_limit or DEFAULT_AGENT_TOOL_CALLS_LIMIT

    if max_iterations is not None:
        request_limit = min(request_limit, max_iterations * 5)
        tool_calls_limit = min(tool_calls_limit, max_iterations * 3)

    return UsageLimits(request_limit=request_limit, tool_calls_limit=tool_calls_limit)


def get_research_limits(max_iterations: int) -> UsageLimits:
    """Tight budget for the tool-using research phase."""
    return UsageLimits(
        request_limit=max_iterations * 2 + 1,
        tool_calls_limit=max_iterations * 2,
    )


def get_synthesis_limits() -> UsageLimits:
    """Single-shot structured output — no tools, small retry budget."""
    return UsageLimits(request_limit=4, tool_calls_limit=0)

def _validate_gateway_api_key(gateway_key: str) -> None:
    if gateway_key.startswith("paig_"):
        raise RuntimeError(
            "PYDANTIC_AI_GATEWAY_API_KEY uses a deprecated legacy gateway key (paig_...). "
            "Create a new Gateway API key in Logfire (https://logfire.pydantic.dev) and set "
            f"PYDANTIC_AI_GATEWAY_API_KEY to the new pylf_v... key. See {GATEWAY_MIGRATION_URL}"
        )


def resolve_agent_model() -> str:
    """Pick the Pydantic AI model string based on configured API keys."""
    settings = get_settings()
    if settings.agent_model:
        return settings.agent_model

    gateway_key = settings.pydantic_ai_gateway_api_key or ""
    if gateway_key:
        _validate_gateway_api_key(gateway_key)
        return DEFAULT_GATEWAY_MODEL

    if settings.anthropic_api_key:
        return DEFAULT_ANTHROPIC_MODEL

    raise RuntimeError(
        "No LLM API key configured. Set PYDANTIC_AI_GATEWAY_API_KEY (from Logfire → Gateway) "
        f"or ANTHROPIC_API_KEY. See {GATEWAY_MIGRATION_URL}"
    )
