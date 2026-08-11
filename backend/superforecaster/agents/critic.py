"""Criteria critic — principle 3.

Standalone: not part of the forecast graph. It runs before a question is forecast at
all, which is the only time fixing the criteria is cheap.

"Ambiguity here silently corrupts everything downstream" — a question whose criteria
cannot be adjudicated produces a forecast that cannot be scored, and the problem is
invisible until resolution day. This agent is what the frontend's "Check resolvable"
button calls, and its rewrite is written straight into the fields being edited.
"""

from __future__ import annotations

from datetime import datetime

from ..config import get_budget, get_model_settings, resolve_agent_model
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from ..deps import ForecastDeps
from ..errors import AgentTimeout
from ..models import CriteriaCritique
from ..observability import run_agent
from ..tools import search_web
from . import attach_budget, withdraw_spent_tools, with_model

INSTRUCTIONS = """You review forecast questions for resolvability. You do not forecast
them — you decide whether they COULD be scored fairly once the date arrives.

THE TEST
Imagine two people who disagree about the outcome, both reading these criteria on the
resolution date with all the facts in front of them. Would they reach the same verdict?
If they could argue, the criteria are not resolvable yet.

WHAT MAKES CRITERIA FAIL
  Vague predicates      "significant", "major", "widely adopted", "successful"
                        — replace with a number and a unit.
  No source             who publishes the number that settles this? Name it.
  No threshold          "growth" vs "growth of at least 10%".
  Ambiguous timing      no timezone, "by end of year" without a date, unclear whether
                        the event or its announcement counts.
  Undefined subject     "the top 3 schools" — by what measure, ranked when?
  Compound conditions   two things joined by "and"/"or" without saying which governs.
  Unfalsifiable         no observable event would settle it either way.

WHAT TO RETURN

Your rewrite goes straight into the author's editor, over the text they wrote. They read
one sentence from you and then look at the new wording. Write for that reader.

  is_resolvable  false if you found anything above that two reasonable people could
                 argue over. Be strict — this is the cheap moment to fix it.
  suggested_criteria
                 a rewritten version that is actually adjudicable. Keep the author's
                 intent; change only what has to change. Name a source, give numbers
                 and units, and state the exact observable event that counts as yes.
                 Return the criteria unchanged when nothing needs changing.
                 This field is pasted over the author's own text, so it holds criteria
                 and nothing else. Never write instructions, questions, or a refusal
                 into it. When the question is too vague to rewrite — you cannot tell
                 what "X" is, and guessing would invent a question they did not ask —
                 return their criteria unchanged and ask for what you need in
                 `what_changed` instead.
  what_changed   one or two sentences naming the edits you made and why each was
                 needed. Quote the phrase you replaced. "Replaced 'significant' with
                 'at least 10%', and named the ONS bulletin as the adjudicator." Empty
                 when you changed nothing. Never a list of complaints — the fix is
                 already applied, so say what it was.
  suggested_resolution_source
                 REQUIRED, always, whether or not you found anything else wrong. The
                 specific publication, dataset, register, or body whose output settles
                 this question on the resolution date. Name it precisely enough that
                 someone could go and read it — "the SEC EDGAR full-text filing search",
                 not "public filings"; "the ONS Consumer Price Inflation bulletin", not
                 "official statistics". A question with no named adjudicator is not
                 resolvable no matter how crisp its wording, so if you cannot name one,
                 say so in `what_changed` and set `is_resolvable` false.

At most TWO searches, and only to check that a source you are about to name exists and
publishes what the criteria assume it does — a criterion resting on a statistic nobody
publishes is not resolvable. That is the only thing worth searching for here. You are
judging the wording of the question, not forecasting it: do not go looking for the
answer, for background on the topic, or for a better source than the one you already
found. Most questions need one search or none. Every tool result tells you what is left.
"""


def build_critic_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, CriteriaCritique]:
    agent = Agent[ForecastDeps, CriteriaCritique](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
        name="critic",
        deps_type=ForecastDeps,
        output_type=CriteriaCritique,
        system_prompt=INSTRUCTIONS,
        tools=[search_web],
        prepare_tools=withdraw_spent_tools,
        retries=1,
    )
    attach_budget(agent)
    return agent


_agent: Agent[ForecastDeps, CriteriaCritique] | None = None


def get_critic_agent() -> Agent[ForecastDeps, CriteriaCritique]:
    global _agent
    if _agent is None:
        _agent = build_critic_agent()
    return _agent


async def run_critique(
    question: str,
    resolution_criteria: str,
    resolution_date: datetime | None = None,
    deps: ForecastDeps | None = None,
) -> CriteriaCritique:
    """Evaluate whether a question could be scored fairly, and suggest a fix."""
    deps = deps or ForecastDeps()
    date_line = (
        f"\nPROPOSED RESOLUTION DATE: {resolution_date.isoformat()}"
        if resolution_date is not None
        else "\nPROPOSED RESOLUTION DATE: (none given — that is itself a finding)"
    )

    prompt = f"""Review this forecast question for resolvability.

QUESTION: {question}

PROPOSED RESOLUTION CRITERIA:
{resolution_criteria}{date_line}

Return a CriteriaCritique."""

    agent = get_critic_agent()
    try:
        with with_model(agent, deps) as bound:
            result = await run_agent(
                bound,
                prompt,
                deps=deps,
                verbose=deps.verbose,
                budget=get_budget(agent.name),
                run_name="criteria critique",
            )
    except (UsageLimitExceeded, AgentTimeout) as exc:
        return _unfinished(resolution_criteria, exc)
    return _require_a_source(result.output)


def _require_a_source(critique: CriteriaCritique) -> CriteriaCritique:
    """A critique that names no resolution source cannot pass.

    Enforced here rather than trusted to the prompt because it is the one gap that
    survives to resolution day silently: criteria can be perfectly crisp and still have
    nobody who adjudicates them, and a forecast nobody can score is a forecast that was
    never worth running. `is_resolvable` is the flag the API and the CLI read, so
    flipping it is what makes "name a source" a requirement rather than a suggestion.

    Nothing is invented — the note says exactly what is absent. It is appended to the
    rewrite rationale rather than replacing it, because the criteria rewrite still
    happened and the reader is about to see it in their editor.
    """
    if critique.suggested_resolution_source.strip():
        return critique

    note = (
        "No resolution source. Name the specific publication, dataset, register, or "
        "body whose output settles this on the resolution date — without one, nobody "
        "can score the forecast."
    )
    already_said = note in critique.what_changed
    return critique.model_copy(
        update={
            "is_resolvable": False,
            "what_changed": (
                critique.what_changed
                if already_said
                else f"{critique.what_changed} {note}".strip()
            ),
        }
    )


def _unfinished(
    resolution_criteria: str, cause: Exception | None = None
) -> CriteriaCritique:
    """What the critic returns when it hit a wall instead of converging.

    Two walls, and they degrade the same way. `UsageLimitExceeded` means it searched too
    many times; `AgentTimeout` means it stopped responding. Neither leaves a partial
    critique to salvage. Raising instead would 500 `/questions/critique`, and the button
    that called it writes its result into fields the reader is still editing — an error
    banner over a half-typed question is a worse answer than a sentence saying the check
    did not finish.

    Nothing is claimed to be wrong with the criteria, because nothing was reviewed. The
    rewrite is the author's own text unchanged, so writing it back is a no-op rather
    than a fabricated suggestion.
    """
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
