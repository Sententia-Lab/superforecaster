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
    """Refuse before the agent is built when no LLM key is configured."""
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
    """Review a draft question for resolvability, and rewrite it. Principle 3."""
    return await run_critique(
        question=body.question,
        resolution_criteria=body.resolution_criteria,
        resolution_date=body.resolution_date,
        resolution_source=body.resolution_source,
    )


@router.post("/draft")
async def draft_question(body: DraftQuestionRequest) -> DraftedQuestion:
    """Parse freeform text into the four fields a forecast needs."""
    try:
        return await run_draft(body.text)
    except AgentTimeout as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        )
