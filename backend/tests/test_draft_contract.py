"""What a drafted question must contain, and what the draft agent may spend.

"Draft with AI" fills all four fields so the forecast is runnable without a second
call. `resolution_source` is the field that makes that true and the one the author
usually never typed, so the schema requires it rather than trusting the prompt.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import config
from superforecaster.agents.draft import build_draft_agent
from superforecaster.models import DraftedQuestion

FIELDS = {
    "question": "Will UK CPI inflation exceed 3% in any month of 2027?",
    "resolution_criteria": "Yes if a 2027 month prints above 3.0% year on year.",
    "resolution_date": datetime(2028, 1, 31, tzinfo=timezone.utc),
    "category": "economics",
}


def test_a_drafted_question_must_name_a_source():
    """An empty source gates "Run now" and `POST /runs` refuses it outright (ADR 44).

    Leaving the field out is a validation error, which pydantic-ai hands back to the
    model as a retry — the prompt asks for a source, and this is what enforces it.
    """
    with pytest.raises(ValidationError):
        DraftedQuestion(**FIELDS)

    drafted = DraftedQuestion(
        **FIELDS, resolution_source="ONS Consumer Price Inflation bulletin"
    )
    assert drafted.resolution_source


def test_the_draft_agent_has_no_tools_to_match_its_budget():
    """Its budget allows zero tool calls, so an attached tool is a trap, not a feature.

    A model that reached for one would raise `UsageLimitExceeded` on a path that
    catches nothing, turning a draft into a 500. It names the adjudicator from what it
    knows; the critic is the agent that searches to check the name.
    """
    agent = build_draft_agent(model="test")
    named = [
        name for toolset in agent.toolsets for name in getattr(toolset, "tools", {})
    ]

    assert config.get_budget("draft").tool_calls == 0
    assert named == []
