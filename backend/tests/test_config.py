import pytest

import config

from config import (
    get_research_limits,
    get_synthesis_limits,
    get_usage_limits,
    resolve_agent_model,
)


def test_resolve_agent_model_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "test:model")
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "pylf_v_test")
    assert resolve_agent_model() == "test:model"


def test_resolve_agent_model_uses_logfire_gateway_key(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "pylf_v_test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_agent_model() == "gateway/anthropic:claude-sonnet-4-6"


def test_resolve_agent_model_falls_back_to_anthropic(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("PYDANTIC_AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert resolve_agent_model() == "anthropic:claude-sonnet-4-6"


def test_get_usage_limits_default(monkeypatch):
    monkeypatch.delenv("AGENT_REQUEST_LIMIT", raising=False)
    monkeypatch.delenv("AGENT_TOOL_CALLS_LIMIT", raising=False)
    limits = get_usage_limits()
    assert limits.request_limit == 40
    assert limits.tool_calls_limit == 20


def test_get_usage_limits_scales_with_max_iterations(monkeypatch):
    monkeypatch.delenv("AGENT_REQUEST_LIMIT", raising=False)
    monkeypatch.delenv("AGENT_TOOL_CALLS_LIMIT", raising=False)
    limits = get_usage_limits(max_iterations=5)
    assert limits.request_limit == 25
    assert limits.tool_calls_limit == 15


def test_get_usage_limits_unlimited_env(monkeypatch):
    monkeypatch.setenv("AGENT_REQUEST_LIMIT", "none")
    monkeypatch.setenv("AGENT_TOOL_CALLS_LIMIT", "none")
    limits = get_usage_limits()
    assert limits.request_limit == 40
    assert limits.tool_calls_limit == 20


def test_get_research_limits_scales_with_max_iterations():
    limits = get_research_limits(5)
    assert limits.request_limit == 16
    assert limits.tool_calls_limit == 15


def test_research_limits_are_tunable_per_iteration(monkeypatch):
    """These were literals at 2 and 2, which capped a default run at ten tool calls —
    reachable in a normal run, and reaching it killed the whole graph."""
    monkeypatch.setenv("RESEARCH_TOOL_CALLS_PER_ITERATION", "6")
    monkeypatch.setenv("RESEARCH_REQUESTS_PER_ITERATION", "4")

    limits = get_research_limits(5)
    assert limits.tool_calls_limit == 30
    assert limits.request_limit == 21


def test_get_synthesis_limits():
    limits = get_synthesis_limits()
    assert limits.request_limit == 4
    assert limits.tool_calls_limit == 0


def test_resolve_agent_model_rejects_legacy_gateway_key(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "paig_old_key")
    with pytest.raises(RuntimeError, match="deprecated legacy gateway key"):
        resolve_agent_model()


def test_cell_budget_puts_the_wall_above_the_cline():
    soft, hard = config.get_cell_budget(5)
    assert soft == 5
    assert hard == 8
    assert hard > soft  # the headroom is where the agent lands its answer


def test_cell_budget_respects_both_env_vars(monkeypatch):
    monkeypatch.setenv("CELL_SOFT_CALLS_PER_ITERATION", "2")
    monkeypatch.setenv("CELL_HARD_HEADROOM", "1")
    assert config.get_cell_budget(4) == (8, 9)


def test_cell_limits_cap_tool_calls_at_the_hard_depth():
    assert config.get_cell_limits(5).tool_calls_limit == 8


def test_a_headroom_of_zero_makes_the_cline_the_wall(monkeypatch):
    """The setting the verification step uses to force degradation on purpose."""
    monkeypatch.setenv("CELL_HARD_HEADROOM", "0")
    soft, hard = config.get_cell_budget(3)
    assert soft == hard == 3
