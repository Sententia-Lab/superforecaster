"""Backend configuration — loads `backend/.env` and exposes typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from pydantic_ai import UsageLimits

_BACKEND_ROOT = Path(__file__).resolve().parent
ENV_FILE = _BACKEND_ROOT / ".env"

# Snapshot which names the real environment already carried, BEFORE the file is read.
# `override=False` means an exported variable beats `.env`, and after `load_dotenv` runs
# the two are indistinguishable in `os.environ` — so "why is this setting not what my .env
# says" becomes unanswerable unless the answer is captured here, first.
_PRESET_ENV: frozenset[str] = frozenset(k for k, v in os.environ.items() if v != "")

load_dotenv(ENV_FILE, override=False)


def origin(name: str) -> str:
    """Where `name`'s value came from: the environment, `.env`, or nowhere."""
    if name in _PRESET_ENV:
        return "environment"
    if os.getenv(name):
        return ".env"
    return "unset"

DEFAULT_GATEWAY_MODEL = "gateway/anthropic:claude-sonnet-4-6"
DEFAULT_ANTHROPIC_MODEL = "anthropic:claude-sonnet-4-6"
GATEWAY_MIGRATION_URL = "https://pydantic.dev/docs/logfire/gateway-migration/"
DEFAULT_AGENT_REQUEST_LIMIT = 40
DEFAULT_AGENT_TOOL_CALLS_LIMIT = 20

# How much budget one unit of `max_iterations` buys a researching agent. These were
# hardcoded at 2 and 2, which meant the default `max_iterations=5` capped the outside
# and inside view at ten tool calls — reachable in a normal run, and hitting it killed
# the whole graph. Configuration rather than a literal, per ADR 14.
DEFAULT_RESEARCH_REQUESTS_PER_ITERATION = 3
DEFAULT_RESEARCH_TOOL_CALLS_PER_ITERATION = 3

DEFAULT_CELL_SOFT_CALLS_PER_ITERATION = 1
DEFAULT_CELL_HARD_HEADROOM = 3


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
    run_max_concurrent: int
    run_event_buffer: int
    run_retention_minutes: int
    run_checkpoint_dir: str
    frontend_dir: str


@dataclass(frozen=True, slots=True)
class CheckThresholds:
    """Every tunable number used by `superforecaster.checks`.

    These are guesses until a backtest says otherwise, so none of them are literals
    in `checks.py`. Each maps to one `CHECK_*` env var.
    """

    reference_class_disagreement: float  # P7  — spread that demands an explanation
    reference_class_agreement: (
        float  # P16 — spread under which classes count as agreeing
    )
    calibration_floor: float  # P16 — lowest unearned probability
    calibration_ceiling: float  # P16 — highest unearned probability
    large_move: float  # P12 — jump that triggers VerifyLargeMove
    derivation_slack: float  # P6  — stated vs implied probability tolerance
    aggregate_slack: float  # P7  — stated vs weight-implied base rate tolerance
    round_number_rate: float  # P8  — run-level rounding rate that gets flagged
    min_probability_delta: float  # P10 — below this an update is noise
    # Cutoffs on the 0..2 mean rank in `checks.aggregate_source_confidence`.
    support_high: float  # at or above this, evidential support reads "high"
    support_medium: float  # at or above this, "medium"; below it, "low"


def _parse_optional_int(
    raw: str | None,
    *,
    unlimited_values: frozenset[str] = frozenset({"none", "unlimited"}),
) -> int | None:
    if raw is None:
        return None
    if raw.lower() in unlimited_values:
        return None
    return int(raw)


def get_settings() -> Settings:
    request_limit = _parse_optional_int(
        os.getenv("AGENT_REQUEST_LIMIT", str(DEFAULT_AGENT_REQUEST_LIMIT))
    )
    tool_calls_limit = _parse_optional_int(
        os.getenv("AGENT_TOOL_CALLS_LIMIT", str(DEFAULT_AGENT_TOOL_CALLS_LIMIT))
    )

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
        run_max_concurrent=int(os.getenv("RUN_MAX_CONCURRENT", "5")),
        run_event_buffer=int(os.getenv("RUN_EVENT_BUFFER", "5000")),
        run_retention_minutes=int(os.getenv("RUN_RETENTION_MINUTES", "60")),
        run_checkpoint_dir=os.getenv("RUN_CHECKPOINT_DIR", "./run_checkpoints"),
        frontend_dir=os.getenv("FRONTEND_DIR", str(_BACKEND_ROOT.parent / "frontend")),
    )


def get_check_thresholds() -> CheckThresholds:
    """Read the CHECK_* env vars, falling back to defaults.

    Re-reads on every call, matching `get_settings()`, so tests can monkeypatch.
    """
    return CheckThresholds(
        reference_class_disagreement=float(os.getenv("CHECK_RC_DISAGREEMENT", "0.20")),
        reference_class_agreement=float(os.getenv("CHECK_RC_AGREEMENT", "0.10")),
        calibration_floor=float(os.getenv("CHECK_CALIBRATION_FLOOR", "0.02")),
        calibration_ceiling=float(os.getenv("CHECK_CALIBRATION_CEILING", "0.98")),
        large_move=float(os.getenv("CHECK_LARGE_MOVE", "0.75")),
        derivation_slack=float(os.getenv("CHECK_DERIVATION_SLACK", "0.05")),
        aggregate_slack=float(os.getenv("CHECK_AGGREGATE_SLACK", "0.05")),
        round_number_rate=float(os.getenv("CHECK_ROUND_NUMBER_RATE", "0.40")),
        min_probability_delta=float(os.getenv("MIN_PROBABILITY_DELTA", "0.03")),
        support_high=float(os.getenv("CHECK_SUPPORT_HIGH", "1.5")),
        support_medium=float(os.getenv("CHECK_SUPPORT_MEDIUM", "0.5")),
    )


def get_model_garden_margin_days() -> int:
    """Safety margin applied to published training cutoffs. See `model_garden`."""
    return int(os.getenv("MODEL_GARDEN_MARGIN_DAYS", "90"))


def get_usage_limits(*, max_iterations: int | None = None) -> UsageLimits:
    settings = get_settings()
    request_limit = settings.agent_request_limit or DEFAULT_AGENT_REQUEST_LIMIT
    tool_calls_limit = settings.agent_tool_calls_limit or DEFAULT_AGENT_TOOL_CALLS_LIMIT

    if max_iterations is not None:
        request_limit = min(request_limit, max_iterations * 5)
        tool_calls_limit = min(tool_calls_limit, max_iterations * 3)

    return UsageLimits(request_limit=request_limit, tool_calls_limit=tool_calls_limit)


def get_cell_budget(max_iterations: int) -> tuple[int, int]:
    """`(soft_depth, hard_depth)` for ONE cell — one column at one research stage.

    Per cell, not per stage. The old `get_research_limits` handed the whole outside view
    `max_iterations * 3` calls to cover three to five sub-questions, and in practice the
    single agent spent them all on the most searchable one.

    Two numbers because one is a wall with no warning. `soft_depth` is the cline: past it
    the agent is told to stop searching and commit. `hard_depth` is
    `UsageLimits.tool_calls_limit`, and the headroom between them is what the agent gets
    to actually land its answer in.

    The defaults are the decision, so here is the arithmetic. At `max_iterations=5` a cell
    gets soft 5 / hard 8. Against today's 15 calls for the whole row:

        3 researchable columns -> 24 worst case (1.6x the calls, ~1/3 the wall-clock)
        4 researchable columns -> 32 worst case (2.1x, ~1/4)
        5 researchable columns -> 40 worst case (2.7x, ~1/5)   <- the case to watch

    and they are spent evenly across the question rather than pooled into one column.
    """
    soft_per = int(
        os.getenv(
            "CELL_SOFT_CALLS_PER_ITERATION", str(DEFAULT_CELL_SOFT_CALLS_PER_ITERATION)
        )
    )
    headroom = int(os.getenv("CELL_HARD_HEADROOM", str(DEFAULT_CELL_HARD_HEADROOM)))
    soft = max(1, max_iterations * soft_per)
    return soft, soft + max(0, headroom)


def get_cell_limits(max_iterations: int) -> UsageLimits:
    """Hard limits for one cell. The wall `get_cell_budget`'s cline sits below.

    Exceeding this still raises `UsageLimitExceeded` — but a cell catches its own and
    degrades to no result, so one greedy column no longer kills the run.
    """
    _, hard = get_cell_budget(max_iterations)
    return UsageLimits(request_limit=hard + 3, tool_calls_limit=hard)


def get_research_limits(max_iterations: int) -> UsageLimits:
    """Budget for a research agent covering the whole question at once.

    Now only the fallback path — a decomposition with nothing researchable — plus the
    component evals. The fanned-out rows use `get_cell_limits`, which is per column.

    Scales with `max_iterations` so a caller asking for deeper research gets it. The
    per-iteration rates are configurable because the right number is a guess until a
    backtest says otherwise — and because exceeding it raises `UsageLimitExceeded`,
    which on this path still kills the run rather than degrading it.
    """
    requests_per = int(
        os.getenv(
            "RESEARCH_REQUESTS_PER_ITERATION",
            str(DEFAULT_RESEARCH_REQUESTS_PER_ITERATION),
        )
    )
    tool_calls_per = int(
        os.getenv(
            "RESEARCH_TOOL_CALLS_PER_ITERATION",
            str(DEFAULT_RESEARCH_TOOL_CALLS_PER_ITERATION),
        )
    )
    return UsageLimits(
        request_limit=max_iterations * requests_per + 1,
        tool_calls_limit=max_iterations * tool_calls_per,
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
