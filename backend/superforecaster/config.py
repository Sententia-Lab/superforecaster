"""Core configuration — every tunable the forecasting logic reads.

Values come from `os.environ` and are re-read on every call, so a test can monkeypatch
one without touching the others.

This module never loads a `.env` file. A library that reads files off disk at import
time cannot be embedded in someone else's program. `app.config.load_env()` does that,
and only an application calls it.

Deployment settings — the database path, the admin key, where the frontend build
lives — are not here. They are in `app.config`, because core has no database, no
authentication, and no frontend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic_ai import UsageLimits


def active_llm_key_name() -> str:
    """Which variable actually credentials the model right now.

    `resolve_agent_model` prefers the gateway, so a panel that always spoke about
    `ANTHROPIC_API_KEY` would report "unset" on a working gateway install and would write
    a key that the gateway then overrules. One row, naming whichever key is in play.
    """
    if os.getenv("PYDANTIC_AI_GATEWAY_API_KEY"):
        return "PYDANTIC_AI_GATEWAY_API_KEY"
    return "ANTHROPIC_API_KEY"


DEFAULT_GATEWAY_MODEL = "gateway/anthropic:claude-sonnet-4-6"
DEFAULT_ANTHROPIC_MODEL = "anthropic:claude-sonnet-4-6"
GATEWAY_MIGRATION_URL = "https://pydantic.dev/docs/logfire/gateway-migration/"

# Output-token ceiling per model response. The provider default (4096 for Anthropic)
# is too small for the synthesize agent's structured output — a full Forecast with
# decompositions, research summary, and rationales is several thousand tokens of JSON,
# and a response truncated mid-tool-call raises IncompleteToolCall, which a retry then
# hits again, forever. One ceiling for every agent: the failure is invisible until the
# largest output crosses it, so per-agent tuning would just move the cliff around.
DEFAULT_AGENT_MAX_TOKENS = 16384

# Wall-clock ceilings. A usage limit bounds how many times an agent may act; it says
# nothing about how long one act may take, and every stall this system has produced was
# a request that never came back rather than a run that acted too often. Without these a
# hung provider call hangs the run forever, and the browser sits on a loading state
# waiting for an `end` frame that is never coming.
#
# The stage ceiling sits above the per-agent one: a gated stage is at most a handful of
# agent calls (synthesis worst case is reflect + two synthesize attempts), so a stage
# that outlives this is stuck, not thorough.
DEFAULT_AGENT_TIMEOUT_SECONDS = 180
DEFAULT_STAGE_TIMEOUT_SECONDS = 600


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


BASELINE_ITERATIONS = 5
"""The `max_iterations` every number in `BUDGETS` is written for. See `Budget.scaled`."""


@dataclass(frozen=True, slots=True)
class Budget:
    """What one agent run may spend, in the four units an agent run spends.

    Four ceilings rather than one because an agent can run away in four different ways,
    and stopping one does not stop the others. A tool-call cap does not stop a model that
    re-reads a growing transcript, and a token cap does not stop a model that searches
    forty times for cheap results.

    `cost_usd` is the only one this codebase enforces itself, in
    `agents.attach_budget`. Pydantic AI enforces the other three, through `limits()`.
    """

    name: str
    cost_usd: float
    tokens: int
    tool_calls: int
    iterations: int
    """Model requests. One iteration is one round of "think, maybe call a tool"."""

    def limits(self) -> UsageLimits:
        """The ceilings Pydantic AI enforces.

        `tool_calls_limit` is deliberately **double** `tool_calls`, because it is no
        longer what stops the searching — `agents.withdraw_spent_tools` is. That hook
        stops offering the search tools the moment `tool_calls` is spent, which a model
        cannot argue with, so the real cap is exact.

        What this headroom absorbs is the last batch. A model that asks for four searches
        in one turn with two left projects six against a ceiling of four, and Pydantic AI
        refuses the **whole batch** before any of it runs — killing a cell that was one
        turn from writing up. Doubling means an overshoot has to be larger than the entire
        budget to be fatal, and withdrawal guarantees there is at most one of them.

        So a run may exceed `tool_calls` by one over-eager turn and no more.
        """
        return UsageLimits(
            request_limit=self.iterations,
            tool_calls_limit=self.tool_calls * 2,
            total_tokens_limit=self.tokens,
        )

    def scaled(self, max_iterations: int) -> Budget:
        """This budget at a different search depth. `max_iterations` is the user's knob.

        All four numbers move together. Scaling only the iteration count would let a
        deeper run reach its token or cost ceiling before it reached the depth the user
        asked for, which reads as a broken setting rather than a budget.
        """
        factor = max(1, max_iterations) / BASELINE_ITERATIONS
        return Budget(
            name=self.name,
            cost_usd=self.cost_usd * factor,
            tokens=int(self.tokens * factor),
            tool_calls=max(1, int(self.tool_calls * factor)),
            iterations=max(2, int(self.iterations * factor)),
        )


BUDGETS: dict[str, Budget] = {
    b.name: b
    for b in (
        # The two research cells. Fanned out per column per stage, so these are the
        # numbers a whole run multiplies by: five columns of three lenses is fifteen.
        Budget(
            "base_rate_cell", cost_usd=0.40, tokens=200_000, tool_calls=8, iterations=11
        ),
        Budget(
            "inside_view",
            cost_usd=0.40,
            tokens=200_000,
            tool_calls=8,
            iterations=11,
        ),
        # Bounded lookups outside the forecast. Each answers one question: is this
        # resolvable, has it resolved, what happened in the last two days, what did the
        # reasoning get wrong.
        Budget("critic", cost_usd=0.10, tokens=60_000, tool_calls=3, iterations=6),
        Budget("resolution", cost_usd=0.10, tokens=60_000, tool_calls=4, iterations=7),
        Budget("update", cost_usd=0.10, tokens=60_000, tool_calls=4, iterations=7),
        Budget("postmortem", cost_usd=0.10, tokens=60_000, tool_calls=4, iterations=7),
        # No-tool steps. `tool_calls=0` is the point: these agents are built with no
        # tools, and a ceiling of zero makes that a fact the runtime enforces rather
        # than a property of how the agent happened to be constructed. The structured
        # answer is not counted — Pydantic AI does not charge the output tool.
        Budget("decompose", cost_usd=0.15, tokens=80_000, tool_calls=0, iterations=4),
        Budget("lenses", cost_usd=0.15, tokens=80_000, tool_calls=0, iterations=4),
        Budget("reflect", cost_usd=0.20, tokens=100_000, tool_calls=0, iterations=4),
        Budget(
            "synthesize",
            cost_usd=0.25,
            tokens=120_000,
            tool_calls=0,
            iterations=4,
        ),
        Budget("draft", cost_usd=0.10, tokens=40_000, tool_calls=0, iterations=4),
    )
}
"""Every agent in the system, and what it may spend. One row per agent, no exceptions.

The numbers are guesses until a backtest says otherwise, so `BUDGET_<NAME>` overrides
any row — `BUDGET_CRITIC="0.10,60000,3,6"`, in the field order of `Budget`.
"""


def get_budget(name: str, *, max_iterations: int | None = None) -> Budget:
    """The named agent's budget, scaled when the caller asked for a deeper run.

    Re-reads the environment on every call, matching `get_settings()`, so a test can
    monkeypatch one agent's ceiling without touching the others.
    """
    budget = BUDGETS[name]
    raw = os.getenv(f"BUDGET_{name.upper()}")
    if raw:
        cost, tokens, tool_calls, iterations = raw.split(",")
        budget = Budget(
            name=name,
            cost_usd=float(cost),
            tokens=int(tokens),
            tool_calls=int(tool_calls),
            iterations=int(iterations),
        )
    return budget if max_iterations is None else budget.scaled(max_iterations)


def get_model_settings() -> dict:
    """Model settings shared by every agent. `AGENT_MAX_TOKENS` overrides the ceiling.

    Read per call, matching the budget getters, so tests can monkeypatch the env var.

    **`parallel_tool_calls=False` — one search per turn.** Pydantic AI's Anthropic model
    turns this into the API's own `disable_parallel_tool_use`, so it is the provider that
    enforces it, not a sentence in a prompt. Both model strings this project resolves —
    `anthropic:…` and `gateway/anthropic:…` — are `AnthropicModel`, so both honour it.

    Two things break when an agent batches its searches:

    1. The budget countdown stops being actionable. `attach_budget` reports what is left
       once per model request, so a turn that spends four searches walks 8 -> 4 with no
       warning in between, and the agent can pass every stop band without seeing one.
    2. A batch is refused **whole**. Four searches requested with two left kills the cell
       outright, before any of the four runs.

    Serial searching costs turns rather than tool calls, and the iteration ceilings have
    room: `base_rate_cell` allows 11 requests for 8 searches, which is one request per
    search plus two to open and close.

    It is a no-op for the agents built without tools — Pydantic AI omits `tool_choice`
    entirely when there are none.
    """
    return {
        "max_tokens": int(os.getenv("AGENT_MAX_TOKENS", str(DEFAULT_AGENT_MAX_TOKENS))),
        "parallel_tool_calls": False,
    }


def get_agent_timeout() -> float:
    """Wall-clock ceiling on one agent run. 0 disables it.

    Separate from the usage limits because they bound different failures. A usage limit
    catches an agent that keeps searching; this catches one that is not doing anything at
    all — a provider request that never returns, a stream that stops mid-token. The
    second failure mode is the one that leaves the browser on a loading state forever,
    because no limit is ever reached and no exception is ever raised.
    """
    return get_settings().agent_timeout_seconds


def get_stage_timeout() -> float:
    """Wall-clock ceiling on one gated stage step. 0 disables it.

    Above the per-agent ceiling rather than instead of it: a handful of agent calls that
    each finish just inside their own timeout still add up to a step nobody is waiting
    for any more. There is no whole-run timeout — a gated run is *supposed* to sit idle
    at a gate indefinitely; only the work between two clicks is bounded.
    """
    return get_settings().stage_timeout_seconds


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
