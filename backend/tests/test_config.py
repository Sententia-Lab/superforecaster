import pytest

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
    assert limits.request_limit == 11
    assert limits.tool_calls_limit == 10


def test_get_synthesis_limits():
    limits = get_synthesis_limits()
    assert limits.request_limit == 4
    assert limits.tool_calls_limit == 0


def test_resolve_agent_model_rejects_legacy_gateway_key(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "paig_old_key")
    with pytest.raises(RuntimeError, match="deprecated legacy gateway key"):
        resolve_agent_model()
