"""Question drafting endpoints — parse freeform text, critique resolvability."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from superforecaster.agents.critic import run_critique
from superforecaster.agents.draft import run_draft
from superforecaster.errors import AgentTimeout
from superforecaster.models import (
    CriteriaCritique,
    CritiqueQuestionRequest,
    DraftQuestionRequest,
    DraftResponse,
)

router = APIRouter(prefix="/questions", tags=["questions"])


@router.post("/critique")
async def critique_question(body: CritiqueQuestionRequest) -> CriteriaCritique:
    """Review a draft question for resolvability. Principle 3.

    Public and stateless — this runs while someone is still typing, which is the only
    point at which fixing ambiguous criteria is cheap. Ambiguity that survives to
    resolution day silently corrupts the score.
    """
    return await run_critique(
        question=body.question,
        resolution_criteria=body.resolution_criteria,
        resolution_date=body.resolution_date,
    )


@router.post("/draft")
async def draft_question(body: DraftQuestionRequest) -> DraftResponse:
    """Parse freeform text into a question, then critique its resolvability.

    Two sequential agent calls. Deliberately not streamed: it is the cheapest step in
    the system, and a spinner is a truthful UI for something that takes seconds.

    A spinner is only truthful while the request is still alive, which is why the parse
    turns a timeout into a 504 rather than letting the connection hang: the frontend
    toasts the failure and gives the reader their text back. The critique half degrades
    instead of raising — see `agents.critic._unfinished` — because there is a parsed
    question to hand back by then and losing it costs the reader more than an unreviewed
    draft does.
    """
    try:
        parsed = await run_draft(body.text)
    except AgentTimeout as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
    critique = await run_critique(
        question=parsed.question,
        resolution_criteria=parsed.resolution_criteria,
        resolution_date=parsed.resolution_date,
    )
    return DraftResponse(parsed=parsed, critique=critique)
