import pytest

from config import BUDGETS, get_budget, resolve_agent_model


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


def test_resolve_agent_model_rejects_legacy_gateway_key(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "paig_old_key")
    with pytest.raises(RuntimeError, match="deprecated legacy gateway key"):
        resolve_agent_model()


# ---------- the four ceilings ----------


def test_a_budget_carries_all_four_ceilings():
    """One agent runs away in four ways, and stopping one does not stop the others. A
    tool-call cap does not stop a model re-reading a growing transcript; a token cap does
    not stop a model searching forty times for cheap results."""
    b = get_budget("base_rate_cell")

    assert (b.cost_usd, b.tokens, b.tool_calls, b.iterations) == (0.40, 200_000, 8, 11)


def test_three_of_the_four_reach_pydantic_ai():
    """Cost is the one this codebase enforces itself, in `agents.attach_budget`."""
    limits = get_budget("critic").limits()

    assert limits.request_limit == 6
    assert limits.tool_calls_limit == 3
    assert limits.total_tokens_limit == 60_000


def test_every_agent_has_a_budget():
    """A missing row is a `KeyError` at the call site rather than an agent quietly
    running on a process-wide default nobody chose."""
    expected = {
        "base_rate_cell",
        "inside_view",
        "critic",
        "resolution",
        "update",
        "postmortem",
        "decompose",
        "lenses",
        "reflect",
        "synthesize",
        "draft",
    }

    assert set(BUDGETS) == expected


def test_no_tool_agents_are_capped_at_zero_tool_calls():
    """decompose, choose-lenses, reflect, synthesize and draft are built with no tools.
    A ceiling of zero is what makes that a fact the runtime enforces rather than a
    property of how the agent happened to be constructed. Pydantic AI does not charge the
    structured answer as a tool call, so zero does not block the run from ending."""
    for name in ("decompose", "lenses", "reflect", "synthesize", "draft"):
        assert get_budget(name).tool_calls == 0


# ---------- scaling and overrides ----------


def test_a_deeper_run_scales_all_four_numbers():
    """Scaling only the iteration count would let a deeper run hit its token or cost
    ceiling before it reached the depth the user asked for."""
    base = get_budget("base_rate_cell")
    deep = get_budget("base_rate_cell", max_iterations=10)

    assert deep.iterations == base.iterations * 2
    assert deep.tool_calls == base.tool_calls * 2
    assert deep.tokens == base.tokens * 2
    assert deep.cost_usd == base.cost_usd * 2


def test_the_baseline_depth_changes_nothing():
    assert get_budget("critic", max_iterations=5) == get_budget("critic")


def test_one_env_var_overrides_one_agent(monkeypatch):
    monkeypatch.setenv("BUDGET_CRITIC", "0.50,90000,6,9")
    b = get_budget("critic")

    assert (b.cost_usd, b.tokens, b.tool_calls, b.iterations) == (0.50, 90_000, 6, 9)
    assert get_budget("draft").tool_calls == 0  # the others are untouched


# ---------- every agent call is bounded ----------


def test_every_agent_run_passes_a_budget():
    """`run_agent` requires a budget, so this is belt and braces — but it is the test that
    names the rule: an agent call site says what it may spend, always."""
    import ast
    import pathlib

    agents_dir = (
        pathlib.Path(__file__).resolve().parent.parent / "superforecaster" / "agents"
    )
    unbounded = []
    for path in sorted(agents_dir.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Name) and fn.id == "run_agent"):
                continue
            if not any(kw.arg == "budget" for kw in node.keywords):
                unbounded.append(path.name)

    assert unbounded == []


def test_every_agent_constructor_sets_model_settings():
    """Every `Agent(...)` must pass `model_settings` — the output-token ceiling.

    The provider default (4096) truncates the synthesize agent's Forecast mid-tool-call
    (`IncompleteToolCall`), and a retry hits the same wall forever. The failure is
    invisible until the largest output crosses the ceiling, so it is enforced at every
    construction site rather than remembered.
    """
    import ast
    import pathlib

    agents_dir = (
        pathlib.Path(__file__).resolve().parent.parent / "superforecaster" / "agents"
    )
    unbounded = []
    for path in sorted(agents_dir.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            is_agent = (isinstance(fn, ast.Name) and fn.id == "Agent") or (
                isinstance(fn, ast.Subscript)
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "Agent"
            )
            if not is_agent:
                continue
            if not any(kw.arg == "model_settings" for kw in node.keywords):
                unbounded.append(path.name)

    assert unbounded == []


def test_model_settings_ceiling_is_configurable(monkeypatch):
    from config import get_model_settings

    assert get_model_settings()["max_tokens"] == 16384
    monkeypatch.setenv("AGENT_MAX_TOKENS", "32000")
    assert get_model_settings()["max_tokens"] == 32000
