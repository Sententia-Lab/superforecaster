"""Community question endpoints — voting, submission, admin moderation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from config import get_settings

from superforecaster import db
from superforecaster.agents.critic import run_critique
from superforecaster.graphs import run_forecast_graph
from superforecaster.models import (
    ApproveQuestionRequest,
    CreateQuestionRequest,
    CriteriaCritique,
    CritiqueQuestionRequest,
    EditQuestionRequest,
    ForecastInput,
    QuestionRecord,
    VoteRequest,
    VoteResponse,
)

from .deps import get_client_ip_hash, require_admin


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


@router.post("", status_code=status.HTTP_201_CREATED)
def create_question(body: CreateQuestionRequest, request: Request) -> QuestionRecord:
    ip_hash = get_client_ip_hash(request)
    try:
        return db.submit_question(
            text=body.text,
            resolution_criteria=body.resolution_criteria,
            proposed_resolution_date=body.proposed_resolution_date,
            ip_hash=ip_hash,
        )
    except db.RateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        )


@router.get("/top-monthly")
def top_monthly() -> list[QuestionRecord]:
    """Top 5 voted pending/approved questions submitted this calendar month."""
    return db.get_top_monthly(n=5)


@router.get("")
def list_questions(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    sort: str = "score",
    limit: int = 50,
    offset: int = 0,
) -> list[QuestionRecord]:
    ip_hash = get_client_ip_hash(request)
    return db.list_questions(
        status=status_filter,  # type: ignore[arg-type]
        sort=sort,
        limit=limit,
        offset=offset,
        requester_ip_hash=ip_hash,
    )


@router.get("/{question_id}")
def get_question(question_id: str, request: Request) -> QuestionRecord:
    ip_hash = get_client_ip_hash(request)
    record = db.get_question(question_id, requester_ip_hash=ip_hash)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="question not found"
        )
    return record


@router.put("/{question_id}")
def edit_question(
    question_id: str, body: EditQuestionRequest, request: Request
) -> QuestionRecord:
    """Edit a question. Caller IP must match submitter unless admin token is supplied."""
    auth = request.headers.get("authorization", "")
    is_admin = False
    expected = get_settings().admin_api_key
    if expected and auth.startswith("Bearer ") and auth[len("Bearer ") :] == expected:
        is_admin = True

    ip_hash = get_client_ip_hash(request)
    try:
        return db.edit_question(
            question_id=question_id,
            ip_hash=ip_hash,
            text=body.text,
            resolution_criteria=body.resolution_criteria,
            proposed_resolution_date=(
                body.proposed_resolution_date if is_admin else None
            ),
            is_admin=is_admin,
        )
    except db.NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="question not found"
        )
    except db.PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except db.StateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(question_id: str, request: Request) -> None:
    ip_hash = get_client_ip_hash(request)
    try:
        db.delete_question(question_id, ip_hash=ip_hash)
    except db.NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="question not found"
        )
    except db.PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except db.StateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{question_id}/vote")
def cast_vote(question_id: str, body: VoteRequest, request: Request) -> VoteResponse:
    if body.vote not in (-1, 1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="vote must be +1 or -1"
        )
    ip_hash = get_client_ip_hash(request)
    try:
        net_score = db.cast_vote(
            question_id=question_id, ip_hash=ip_hash, vote=body.vote
        )
    except db.NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="question not found"
        )
    return VoteResponse(
        question_id=question_id, net_score=net_score, user_vote=body.vote
    )


@router.delete("/{question_id}/vote")
def undo_vote(question_id: str, request: Request) -> VoteResponse:
    ip_hash = get_client_ip_hash(request)
    net_score = db.remove_vote(question_id=question_id, ip_hash=ip_hash)
    return VoteResponse(question_id=question_id, net_score=net_score, user_vote=None)


@router.post("/{question_id}/approve")
def approve_question(
    question_id: str,
    body: ApproveQuestionRequest,
    _: None = Depends(require_admin),
) -> QuestionRecord:
    try:
        return db.approve_question(
            question_id=question_id,
            resolution_date=body.resolution_date,
            resolution_criteria=body.resolution_criteria,
        )
    except db.NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="question not found"
        )
    except db.StateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{question_id}/reject")
def reject_question(
    question_id: str, _: None = Depends(require_admin)
) -> QuestionRecord:
    try:
        return db.reject_question(question_id)
    except db.NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="question not found"
        )
    except db.StateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/{question_id}/forecast")
async def forecast_from_question(
    question_id: str, _: None = Depends(require_admin)
) -> QuestionRecord:
    """Run the forecast agent on an approved question, link the result back."""
    record = db.get_question(question_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="question not found"
        )
    if record.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"question must be approved (currently {record.status})",
        )

    forecast, _violations = await run_forecast_graph(
        ForecastInput(
            question=record.text,
            resolution_criteria=record.resolution_criteria,
            resolution_date=record.proposed_resolution_date,
            category="community",
        )
    )
    fid = db.save_forecast(forecast, resolution_source="community submission")
    return db.link_question_to_forecast(question_id=question_id, forecast_id=fid)
