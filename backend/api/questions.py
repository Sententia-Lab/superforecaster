"""Question drafting endpoints — parse freeform text, critique resolvability."""

from __future__ import annotations

from superforecaster.config import resolve_agent_model
from fastapi import APIRouter, Depends, HTTPException, status

from superforecaster.agents.critic import run_critique
from superforecaster.agents.draft import run_draft
from superforecaster.errors import AgentTimeout
from superforecaster.models import (
    CriteriaCritique,
    CritiqueQuestionRequest,
    DraftedQuestion,
    DraftQuestionRequest,
)


def require_a_model() -> None:
    """Refuse before the agent is built when no LLM key is configured.

    `resolve_agent_model` raises `RuntimeError` naming the variable to set. Reaching the
    agent with it unhandled turns that sentence into a 500 and an "Internal Server Error"
    banner, which sends the reader to the server log to learn they need a key. These are
    the first two endpoints anyone touches, so they are where the message has to survive.
    """
    try:
        resolve_agent_model()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )


router = APIRouter(
    prefix="/questions",
    tags=["questions"],
    dependencies=[Depends(require_a_model)],
)


@router.post("/critique")
async def critique_question(body: CritiqueQuestionRequest) -> CriteriaCritique:
    """Review a draft question for resolvability, and rewrite it. Principle 3.

    Public and stateless — this runs while someone is still typing, which is the only
    point at which fixing ambiguous criteria is cheap. Ambiguity that survives to
    resolution day silently corrupts the score.

    The caller writes `suggested_criteria` and `suggested_resolution_source` straight
    into the fields being edited and shows `what_changed` beneath them, so the response
    is a replacement rather than a report. Every field it overwrites is also a field it
    reads, `resolution_source` included — the critic verifies the adjudicator the author
    named rather than replacing it with one chosen blind.
    """
    return await run_critique(
        question=body.question,
        resolution_criteria=body.resolution_criteria,
        resolution_date=body.resolution_date,
        resolution_source=body.resolution_source,
    )


@router.post("/draft")
async def draft_question(body: DraftQuestionRequest) -> DraftedQuestion:
    """Parse freeform text into the four fields a forecast needs.

    One agent call. Deliberately not streamed: it is the cheapest step in the system,
    and a spinner is a truthful UI for something that takes seconds.

    Extraction only. The resolvability review is a second call the reader asks for by
    pressing "Check resolvable", so a slow critic no longer sits between someone and the
    question they just typed.

    A timeout becomes a 504 rather than hanging the connection: the frontend shows the
    failure and gives the reader their text back.
    """
    try:
        return await run_draft(body.text)
    except AgentTimeout as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
