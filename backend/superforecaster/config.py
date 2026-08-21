"""Every tunable the forecasting library reads. Values come from `os.environ` on each
call, so a test can monkeypatch one. This module never loads a `.env` file."""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic_ai import UsageLimits

DEFAULT_GATEWAY_MODEL = "gateway/anthropic:claude-sonnet-4-6"
DEFAULT_ANTHROPIC_MODEL = "anthropic:claude-sonnet-4-6"
GATEWAY_MIGRATION_URL = "https://pydantic.dev/docs/logfire/gateway-migration/"

DEFAULT_AGENT_MAX_TOKENS = 8192
DEFAULT_AGENT_TIMEOUT_SECONDS = 360
DEFAULT_STAGE_TIMEOUT_SECONDS = 600

MAX_SEARCH_DEPTH = 50
"""Ceiling on a retried step's `max_iterations` override."""


def active_llm_key_name() -> str:
    """The environment variable that credentials the model right now."""
    if os.getenv("PYDANTIC_AI_GATEWAY_API_KEY"):
        return "PYDANTIC_AI_GATEWAY_API_KEY"
    return "ANTHROPIC_API_KEY"


@dataclass(frozen=True, slots=True)
class Settings:
    pydantic_ai_gateway_api_key: str | None
    anthropic_api_key: str | None
    tavily_api_key: str | None
    wikipedia_api_key: str | None
    logfire_token: str | None
    agent_model: str | None
    min_probability_delta: float
    search_lookback_hours: int
    agent_timeout_seconds: float
    stage_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class CheckThresholds:
    """Every tunable number `checks.py` uses. One `CHECK_*` env var each (ADR 14)."""

    reference_class_disagreement: float  # P7  — spread that demands an explanation
    reference_class_agreement: float  # P16 — spread under which classes agree
    calibration_floor: float  # P16 — lowest unearned probability
    calibration_ceiling: float  # P16 — highest unearned probability
    large_move: float  # P12 — jump that triggers verification
    derivation_slack: float  # P6  — stated vs implied probability tolerance
    aggregate_slack: float  # P7  — stated vs implied base rate tolerance
    min_probability_delta: float  # P10 — below this an update is noise


def get_settings() -> Settings:
    return Settings(
        pydantic_ai_gateway_api_key=os.getenv("PYDANTIC_AI_GATEWAY_API_KEY"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
        tavily_api_key=os.getenv("TAVILY_API_KEY"),
        wikipedia_api_key=os.getenv("WIKIPEDIA_API_KEY"),
        logfire_token=os.getenv("LOGFIRE_TOKEN"),
        agent_model=os.getenv("AGENT_MODEL"),
        min_probability_delta=float(os.getenv("MIN_PROBABILITY_DELTA", "0.03")),
        search_lookback_hours=int(os.getenv("SEARCH_LOOKBACK_HOURS", "48")),
        agent_timeout_seconds=float(
            os.getenv("AGENT_TIMEOUT_SECONDS", str(DEFAULT_AGENT_TIMEOUT_SECONDS))
        ),
        stage_timeout_seconds=float(
            os.getenv("STAGE_TIMEOUT_SECONDS", str(DEFAULT_STAGE_TIMEOUT_SECONDS))
        ),
    )


def get_check_thresholds() -> CheckThresholds:
    return CheckThresholds(
        reference_class_disagreement=float(os.getenv("CHECK_RC_DISAGREEMENT", "0.20")),
        reference_class_agreement=float(os.getenv("CHECK_RC_AGREEMENT", "0.10")),
        calibration_floor=float(os.getenv("CHECK_CALIBRATION_FLOOR", "0.02")),
        calibration_ceiling=float(os.getenv("CHECK_CALIBRATION_CEILING", "0.98")),
        large_move=float(os.getenv("CHECK_LARGE_MOVE", "0.75")),
        derivation_slack=float(os.getenv("CHECK_DERIVATION_SLACK", "0.05")),
        aggregate_slack=float(os.getenv("CHECK_AGGREGATE_SLACK", "0.05")),
        min_probability_delta=float(os.getenv("MIN_PROBABILITY_DELTA", "0.03")),
    )


BASELINE_ITERATIONS = 5
"""The `max_iterations` every row in `BUDGETS` is written for."""

TOOL_CALL_HEADROOM = 4
"""Tool calls the enforced ceiling allows past the budget, for one batched turn."""


@dataclass(frozen=True, slots=True)
class Budget:
    """What one agent run may spend.

    `agents.withdraw_tools` is what caps searching: it stops offering the tools once
    `tool_calls` is spent. Pydantic AI's `tool_calls_limit` is a backstop with headroom,
    because a model can still batch several calls into one turn and a batch is refused
    whole (ADR 81).
    """

    name: str
    tool_calls: int
    requests: int
    tokens: int

    def limits(self) -> UsageLimits:
        return UsageLimits(
            request_limit=self.requests,
            tool_calls_limit=self.tool_calls + TOOL_CALL_HEADROOM,
            total_tokens_limit=self.tokens,
        )

    def scaled(self, max_iterations: int) -> Budget:
        """This budget at a different search depth. `requests` keeps its gap above
        `tool_calls`, so a deeper run still has turns left to answer in."""
        factor = max(1, max_iterations) / BASELINE_ITERATIONS
        tool_calls = int(self.tool_calls * factor)
        return Budget(
            name=self.name,
            tool_calls=tool_calls,
            requests=tool_calls + (self.requests - self.tool_calls),
            tokens=int(self.tokens * factor),
        )


BUDGETS: dict[str, Budget] = {
    b.name: b
    for b in (
        Budget("base_rate_cell", tool_calls=8, requests=12, tokens=200_000),
        Budget("inside_view", tool_calls=8, requests=12, tokens=200_000),
        Budget("critic", tool_calls=4, requests=7, tokens=60_000),
        Budget("resolution", tool_calls=5, requests=8, tokens=60_000),
        Budget("update", tool_calls=5, requests=8, tokens=60_000),
        Budget("postmortem", tool_calls=5, requests=8, tokens=60_000),
        Budget("decompose", tool_calls=0, requests=4, tokens=40_000),
        Budget("lenses", tool_calls=0, requests=4, tokens=40_000),
        Budget("reflect", tool_calls=3, requests=6, tokens=100_000),
        Budget("synthesize", tool_calls=4, requests=8, tokens=120_000),
        Budget("draft", tool_calls=0, requests=4, tokens=40_000),
    )
}
"""One row per agent. `BUDGET_<NAME>="tool_calls,requests,tokens"` overrides a row."""


def get_budget(name: str, *, max_iterations: int | None = None) -> Budget:
    budget = BUDGETS[name]
    raw = os.getenv(f"BUDGET_{name.upper()}")
    if raw:
        tool_calls, requests, tokens = raw.split(",")
        budget = Budget(name, int(tool_calls), int(requests), int(tokens))
    return budget if max_iterations is None else budget.scaled(max_iterations)


def get_model_settings() -> dict:
    """Settings shared by every agent."""
    return {
        "max_tokens": int(os.getenv("AGENT_MAX_TOKENS", str(DEFAULT_AGENT_MAX_TOKENS))),
        "parallel_tool_calls": False,
        "anthropic_cache_tool_definitions": True,
        "anthropic_cache_instructions": True,
        "anthropic_cache": True,
    }


def get_agent_timeout() -> float:
    """Wall-clock ceiling on one agent run. 0 disables it."""
    return get_settings().agent_timeout_seconds


def get_stage_timeout() -> float:
    """Wall-clock ceiling on one gated stage step. 0 disables it."""
    return get_settings().stage_timeout_seconds


def resolve_agent_model() -> str:
    """The Pydantic AI model string, from `AGENT_MODEL` or whichever key is set."""
    settings = get_settings()
    if settings.agent_model:
        return settings.agent_model

    gateway_key = settings.pydantic_ai_gateway_api_key or ""
    if gateway_key:
        if gateway_key.startswith("paig_"):
            raise RuntimeError(
                "PYDANTIC_AI_GATEWAY_API_KEY uses a deprecated legacy gateway key (paig_...). "
                f"Create a new pylf_v... key. See {GATEWAY_MIGRATION_URL}"
            )
        return DEFAULT_GATEWAY_MODEL

    if settings.anthropic_api_key:
        return DEFAULT_ANTHROPIC_MODEL

    raise RuntimeError(
        "No LLM API key configured. Set PYDANTIC_AI_GATEWAY_API_KEY (from Logfire → Gateway) "
        f"or ANTHROPIC_API_KEY. See {GATEWAY_MIGRATION_URL}"
    )
