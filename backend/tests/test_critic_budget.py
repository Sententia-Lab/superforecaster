"""The criteria critic's cline, its wall, and what it returns when it hits the wall.

The critic ran on the process-wide default of 20 tool calls with no cline at all, which
made `UsageLimitExceeded` reachable on an ordinary draft — and since `/questions/draft`
returns the parsed question and the critique from one call, that exception took the
user's parsed draft down with it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from pydantic_ai.exceptions import UsageLimitExceeded

import config
from superforecaster.agents import critic
from superforecaster.models import CriteriaCritique

CRITIQUE = CriteriaCritique(
    is_resolvable=True,
    ambiguities=[],
    missing=[],
    suggested_criteria="unchanged",
)


class _Result:
    output = CRITIQUE


def _capture(monkeypatch) -> dict:
    """Run the critic against a fake `run_agent`, returning what it was called with."""
    seen: dict = {}

    async def fake_run_agent(agent, prompt, **kwargs):
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(critic, "run_agent", fake_run_agent)
    return seen


# ---------- the budget ----------


def test_critique_budget_puts_the_wall_above_the_cline():
    soft, hard = config.get_critique_budget()
    assert soft == 3
    assert hard == 5
    assert hard > soft  # the headroom is where it writes the critique down


def test_critique_budget_respects_both_env_vars(monkeypatch):
    monkeypatch.setenv("CRITIQUE_SOFT_CALLS", "2")
    monkeypatch.setenv("CRITIQUE_HARD_HEADROOM", "1")
    assert config.get_critique_budget() == (2, 3)


def test_critique_limits_cap_tool_calls_well_below_the_global_default():
    limits = config.get_critique_limits()
    assert limits.tool_calls_limit == 5
    assert limits.tool_calls_limit < config.DEFAULT_AGENT_TOOL_CALLS_LIMIT


# ---------- what the run gets ----------


async def test_the_critic_runs_on_its_own_budget_not_the_global_default(monkeypatch):
    seen = _capture(monkeypatch)
    await critic.run_critique("Will X happen?", "X is significant.")

    assert seen["usage_limits"].tool_calls_limit == 5


async def test_the_critic_installs_a_budget_so_the_cline_can_fire(monkeypatch):
    """Without this the tool notices and the dynamic instruction are both silent —
    `ForecastDeps()` arrives from the API with `budget=None`."""
    seen = _capture(monkeypatch)
    await critic.run_critique("Will X happen?", "X is significant.")

    budget = seen["deps"].budget
    assert budget is not None
    assert (budget.soft_depth, budget.hard_depth) == (3, 5)


async def test_the_cline_sits_below_the_wall_the_run_is_given(monkeypatch):
    """The gap is the point: a wall with no warning kills the agent mid-thought."""
    seen = _capture(monkeypatch)
    await critic.run_critique("Will X happen?", "X is significant.")

    assert seen["deps"].budget.soft_depth < seen["usage_limits"].tool_calls_limit


# ---------- the wall ----------


async def test_hitting_the_wall_degrades_instead_of_raising(monkeypatch):
    async def blow_the_budget(agent, prompt, **kwargs):
        raise UsageLimitExceeded("tool_calls_limit of 5 exceeded")

    monkeypatch.setattr(critic, "run_agent", blow_the_budget)
    out = await critic.run_critique("Will X happen?", "X is at least 10% by 2027-01-01.")

    assert isinstance(out, CriteriaCritique)
    assert out.is_resolvable is False
    assert out.suggested_criteria == "X is at least 10% by 2027-01-01."
    assert out.ambiguities == []
    assert any("search budget" in m for m in out.missing)


async def test_a_degraded_critique_invents_no_findings(monkeypatch):
    """It says the check did not finish. It does not claim the criteria are bad."""

    async def blow_the_budget(agent, prompt, **kwargs):
        raise UsageLimitExceeded("tool_calls_limit of 5 exceeded")

    monkeypatch.setattr(critic, "run_agent", blow_the_budget)
    out = await critic.run_critique("Will X happen?", "original text")

    assert out.suggested_resolution_source == ""
    assert "unreviewed" in " ".join(out.missing)


def test_the_draft_endpoint_keeps_the_parsed_question(monkeypatch):
    """The symptom that started this: a 500 here dropped the user back to an empty box.

    `/questions/draft` returns the parsed question and its critique from one call, so a
    critic that raises costs the user the text they just typed.
    """
    from fastapi.testclient import TestClient

    from api import questions as questions_api
    from api.main import app
    from superforecaster.models import DraftedQuestion

    async def fake_draft(text: str) -> DraftedQuestion:
        return DraftedQuestion(
            question="Will X happen by 2027?",
            resolution_criteria="original text",
            resolution_date=datetime(2027, 1, 1, tzinfo=timezone.utc),
            category="general",
        )

    async def blow_the_budget(agent, prompt, **kwargs):
        raise UsageLimitExceeded("tool_calls_limit of 5 exceeded")

    monkeypatch.setattr(questions_api, "run_draft", fake_draft)
    monkeypatch.setattr(critic, "run_agent", blow_the_budget)

    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as client:
        response = client.post("/questions/draft", json={"text": "x" * 40})

    assert response.status_code == 200
    body = response.json()
    assert body["parsed"]["question"] == "Will X happen by 2027?"
    assert body["critique"]["is_resolvable"] is False


@asynccontextmanager
async def _noop_lifespan(app):
    from superforecaster import db

    db.init_db()
    yield
