"""Criteria critic — principle 3. Runs while a question is drafted, when fixing
ambiguity is cheap. Its rewrite is written straight into the editor."""

from __future__ import annotations

from datetime import datetime

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.exceptions import UsageLimitExceeded

from ..config import get_budget
from ..deps import ForecastDeps
from ..errors import AgentTimeout
from ..models import CriteriaCritique
from ..runner import run_agent
from ..tools import extract_pages, search_research, search_web
from . import withdraw_tools

INSTRUCTIONS = """You review forecast questions for resolvability. You do not forecast
them — you decide whether they COULD be scored fairly once the date arrives.

THE TEST: two people who disagree about the outcome read these criteria on the
resolution date with all the facts. Would they reach the same verdict? If they could
argue, the criteria are not resolvable yet.

WHAT MAKES CRITERIA FAIL
  Vague predicates      "significant", "major", "widely adopted" — use a number and unit
  No source             who publishes the number that settles this? Name it.
  No threshold          "growth" vs "growth of at least 10%"
  Ambiguous timing      no timezone, "by end of year", event vs announcement
  Undefined subject     "the top 3 schools" — by what measure, ranked when?
  Compound conditions   "and"/"or" without saying which governs
  Unfalsifiable         no observable event would settle it

Keep the author's intent; change only what must change. `suggested_criteria` is pasted
over their text, so it holds criteria and nothing else — when the question is too vague
to rewrite, return it unchanged and ask for what you need in `what_changed`. Quote the
phrase you replaced in `what_changed`. Always name a `suggested_resolution_source`: judge
the author's source first and return it unchanged when it settles the question.

At most two searches, and only to check that a source you are about to name exists and
publishes what the criteria assume. Do not look for the answer."""

agent = Agent[ForecastDeps, CriteriaCritique](
    name="critic",
    deps_type=ForecastDeps,
    output_type=CriteriaCritique,
    instructions=INSTRUCTIONS,
    tools=[search_research, search_web, extract_pages],
    capabilities=[Hooks(prepare_tools=withdraw_tools)],
    retries=1,
)


async def run_critique(
    question: str,
    resolution_criteria: str,
    resolution_date: datetime | None = None,
    deps: ForecastDeps | None = None,
    resolution_source: str | None = None,
) -> CriteriaCritique:
    """Judge whether a question could be scored fairly, and rewrite it. Degrades to
    "nothing changed" on a timeout or a spent budget rather than raising."""
    deps = deps or ForecastDeps()
    date_line = (
        resolution_date.isoformat()
        if resolution_date is not None
        else "(none given — that is itself a finding)"
    )
    source_line = (
        resolution_source.strip()
        if resolution_source and resolution_source.strip()
        else "(none given — name one)"
    )
    prompt = f"""Review this forecast question for resolvability.

QUESTION: {question}

PROPOSED RESOLUTION CRITERIA:
{resolution_criteria}
PROPOSED RESOLUTION DATE: {date_line}

PROPOSED RESOLUTION SOURCE: {source_line}"""
    try:
        result = await run_agent(
            agent,
            prompt,
            deps=deps,
            budget=get_budget("critic"),
            run_name="criteria critique",
        )
    except (UsageLimitExceeded, AgentTimeout) as exc:
        return _unfinished(resolution_criteria, exc)
    return _require_a_source(result.output)


def _require_a_source(critique: CriteriaCritique) -> CriteriaCritique:
    """A critique that names no resolution source cannot pass (ADR 44)."""
    if critique.suggested_resolution_source.strip():
        return critique
    note = (
        "No resolution source. Name the specific publication, dataset, register, or "
        "body whose output settles this on the resolution date — without one, nobody "
        "can score the forecast."
    )
    what_changed = (
        critique.what_changed
        if note in critique.what_changed
        else f"{critique.what_changed} {note}".strip()
    )
    return critique.model_copy(
        update={"is_resolvable": False, "what_changed": what_changed}
    )


def _unfinished(resolution_criteria: str, cause: Exception) -> CriteriaCritique:
    why = (
        "stopped responding"
        if isinstance(cause, AgentTimeout)
        else "ran out of search budget"
    )
    return CriteriaCritique(
        is_resolvable=False,
        what_changed=(
            f"Nothing changed. The resolvability check {why} before it reached a "
            "verdict, so these criteria are unreviewed rather than known to be wrong. "
            "Name a resolution source yourself, or run the check again."
        ),
        suggested_criteria=resolution_criteria,
        suggested_resolution_source="",
    )
