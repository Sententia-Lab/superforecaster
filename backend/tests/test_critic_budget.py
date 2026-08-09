"""The criteria critic's budget, and what it returns when it blows one of its ceilings.

The critic ran on the process-wide default of 20 tool calls, which made
`UsageLimitExceeded` reachable on an ordinary draft — and since `/questions/draft`
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


def test_the_critic_gets_three_searches_total():
    """A resolvability review is one or two lookups — checking that a source it is about
    to name exists. Anything past that is the critic drifting into forecasting the
    question, which its own prompt forbids and its budget should not fund."""
    b = config.get_budget("critic")

    assert b.tool_calls == 3
    assert b.iterations == 6


async def test_the_critic_runs_on_its_own_budget_not_a_shared_default(monkeypatch):
    seen = _capture(monkeypatch)
    await critic.run_critique("Will X happen?", "X is significant.")

    assert seen["budget"].name == "critic"
    assert seen["budget"].tool_calls == 3


# ---------- the wall ----------


async def test_hitting_the_wall_degrades_instead_of_raising(monkeypatch):
    async def blow_the_budget(agent, prompt, **kwargs):
        raise UsageLimitExceeded("tool_calls_limit of 5 exceeded")

    monkeypatch.setattr(critic, "run_agent", blow_the_budget)
    out = await critic.run_critique(
        "Will X happen?", "X is at least 10% by 2027-01-01."
    )

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


async def test_a_timeout_degrades_the_same_way_the_wall_does(monkeypatch):
    """Two ways to fail, one way to degrade. `UsageLimitExceeded` means it searched too
    often; `AgentTimeout` means it stopped responding. Either way there is a parsed
    question to hand back, and raising throws it away."""
    from superforecaster.errors import AgentTimeout

    async def stall(agent, prompt, **kwargs):
        raise AgentTimeout("criteria critique exceeded its 180s deadline")

    monkeypatch.setattr(critic, "run_agent", stall)
    out = await critic.run_critique("Will X happen?", "original text")

    assert out.is_resolvable is False
    assert any("stopped responding" in m for m in out.missing)
    assert out.suggested_criteria == "original text"


# ---------- the resolution source ----------


async def test_a_critique_naming_no_source_cannot_pass(monkeypatch):
    """Criteria can be perfectly crisp and still name nobody who adjudicates them. A
    forecast nobody can score is a whole run spent for nothing, and the gap only becomes
    visible on the one day it is too late to fix."""
    _capture(monkeypatch)  # returns CRITIQUE: is_resolvable=True, no source
    out = await critic.run_critique(
        "Will X happen?", "X is at least 10% by 2027-01-01."
    )

    assert out.is_resolvable is False
    assert any("No resolution source" in m for m in out.missing)


async def test_a_critique_that_names_a_source_is_left_alone(monkeypatch):
    """The gate must not fire on a critique that did its job."""
    sourced = CRITIQUE.model_copy(
        update={"suggested_resolution_source": "ONS Consumer Price Inflation bulletin"}
    )

    async def fake_run_agent(agent, prompt, **kwargs):
        class R:
            output = sourced

        return R()

    monkeypatch.setattr(critic, "run_agent", fake_run_agent)
    out = await critic.run_critique(
        "Will X happen?", "X is at least 10% by 2027-01-01."
    )

    assert out.is_resolvable is True
    assert out.missing == []


async def test_the_source_finding_is_not_duplicated_on_a_second_pass(monkeypatch):
    """`_require_a_source` is idempotent — a critique that already carries the finding
    must not accumulate a second copy of it."""
    once = critic._require_a_source(CRITIQUE)
    twice = critic._require_a_source(once)

    assert once.missing == twice.missing


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
