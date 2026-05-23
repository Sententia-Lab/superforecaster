"""All Pydantic models shared across agents, db, and api.

Models are grouped by concern:
- Forecast inputs / agent outputs
- Refresh + Resolution agent outputs
- DB record types (rows + computed)
- API request / response shapes
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Confidence = Literal["low", "medium", "high"]
QuestionStatus = Literal["pending", "approved", "rejected", "forecasted"]


# ---------- Forecast agent ----------


class HistoricalAnalog(BaseModel):
    """An analogous historical event used to build the empirical base rate."""

    description: str = Field(description="Brief description of the analogous event")
    outcome: float = Field(ge=0.0, le=1.0, description="0.0 or 1.0")
    relevance: str = Field(description="Why this analog applies to the current question")


class SubPrediction(BaseModel):
    """One component of a Fermi-ized decomposition."""

    question: str = Field(description="Specific, testable sub-question")
    probability: float = Field(ge=0.0, le=1.0)
    rationale: str
    confidence: Confidence


class ResearchSummary(BaseModel):
    """Outside-view + inside-view research findings.

    `historical_analogs` and `empirical_base_rate` are the outside view —
    populated via replanning iterations searching for similar past events.
    """

    historical_analogs: list[HistoricalAnalog] = Field(default_factory=list)
    empirical_base_rate: Optional[float] = Field(
        default=None, description="mean(analog outcomes); None if < 3 analogs found"
    )
    base_rate_note: str = Field(
        default="", description="Caveat on base rate quality/applicability"
    )
    causal_forces: list[str] = Field(default_factory=list)
    evidence: dict[str, list[str]] = Field(
        default_factory=lambda: {"supporting": [], "contradicting": []}
    )
    uncertainties: list[str] = Field(default_factory=list)


class Forecast(BaseModel):
    """Full structured output from forecast_agent."""

    question: str
    resolution_criteria: str
    resolution_date: datetime
    category: str
    probability: float = Field(ge=0.0, le=1.0)
    confidence: Confidence
    decompositions: list[SubPrediction] = Field(min_length=3, max_length=5)
    research: ResearchSummary
    reasoning: str


class ForecastInput(BaseModel):
    """What the forecast_agent receives."""

    question: str
    resolution_criteria: str
    resolution_date: datetime
    category: str
    max_iterations: int = 5


class ForecastResearchNotes(BaseModel):
    """Phase-1 output — research and draft decomposition, no final probability yet."""

    decompositions: list[SubPrediction] = Field(min_length=1, max_length=5)
    research: ResearchSummary
    analysis_notes: str = Field(
        description="Working notes for synthesis: drivers, uncertainties, evidence gaps"
    )


# ---------- Refresh agent ----------


class ForecastRefreshResult(BaseModel):
    """Output from refresh_agent — purely about probability updates."""

    should_update: bool
    new_probability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    new_confidence: Optional[Confidence] = None
    reasoning: str
    evidence_found: list[str] = Field(default_factory=list)


# ---------- Resolution agent ----------


class ResolutionCheckResult(BaseModel):
    """Output from resolution_agent — binary classification."""

    appears_resolved: bool
    suggested_outcome: Optional[float] = Field(default=None, description="0.0 or 1.0")
    confidence: Confidence
    resolution_evidence: Optional[str] = None
    reasoning: str


# ---------- DB record types ----------


class ForecastUpdateRecord(BaseModel):
    """One probability update row."""

    id: str
    forecast_id: str
    probability: float
    confidence: Confidence
    reasoning: str
    is_late: bool
    created_at: datetime


class ForecastRecord(BaseModel):
    """A forecast plus its full update history."""

    id: str
    question: str
    resolution_criteria: str
    resolution_source: str
    category: str
    submission_gap_days: int
    submission_deadline: datetime
    resolution_date: datetime
    resolved_at: Optional[datetime] = None
    outcome: Optional[float] = None
    is_ambiguous: bool = False
    scored_probability: Optional[float] = None
    brier_score: Optional[float] = None
    last_refreshed_at: Optional[datetime] = None
    flagged_for_resolution_review: bool = False
    initial_reasoning: str
    decompositions: list[SubPrediction]
    research: ResearchSummary
    updates: list[ForecastUpdateRecord]
    created_at: datetime


class QuestionRecord(BaseModel):
    """A community-submitted question idea (with computed net_score)."""

    id: str
    text: str
    resolution_criteria: str
    proposed_resolution_date: datetime
    net_score: int
    user_vote: Optional[int] = None  # populated when caller IP is known
    is_own: bool = False  # True if caller IP matches submitter IP
    status: QuestionStatus
    edited_at: Optional[datetime] = None
    is_deleted: bool = False
    created_at: datetime
    approved_at: Optional[datetime] = None
    forecast_id: Optional[str] = None


# ---------- Calibration ----------


class CalibrationBucket(BaseModel):
    range: str  # e.g. "0-10%"
    predicted_avg: float
    actual_frequency: float
    count: int


class CalibrationReport(BaseModel):
    aggregate_brier_score: Optional[float] = None
    total_resolved: int = 0
    total_ambiguous_excluded: int = 0
    buckets: list[CalibrationBucket] = Field(default_factory=list)


# ---------- Refresh job summary ----------


class RefreshSummary(BaseModel):
    total_checked: int = 0
    total_updated: int = 0
    total_skipped: int = 0
    total_flagged_for_review: int = 0
    errors: list[str] = Field(default_factory=list)


# ---------- API request bodies ----------


class CreateForecastRequest(BaseModel):
    question: str
    resolution_criteria: str
    resolution_source: str
    resolution_date: datetime
    category: str
    submission_gap_days: int = 7


class AddUpdateRequest(BaseModel):
    probability: float = Field(ge=0.0, le=1.0)
    confidence: Confidence
    reasoning: str


class ResolveRequest(BaseModel):
    outcome: Optional[float] = Field(
        default=None, description="0.0, 1.0, or None for ambiguous"
    )


class CreateQuestionRequest(BaseModel):
    text: str
    resolution_criteria: str
    proposed_resolution_date: datetime


class EditQuestionRequest(BaseModel):
    text: Optional[str] = None
    resolution_criteria: Optional[str] = None
    proposed_resolution_date: Optional[datetime] = None  # admin-only field


class VoteRequest(BaseModel):
    vote: int = Field(description="+1 (upvote) or -1 (downvote)")


class ApproveQuestionRequest(BaseModel):
    resolution_date: Optional[datetime] = None
    resolution_criteria: Optional[str] = None


# ---------- API responses ----------


class VoteResponse(BaseModel):
    question_id: str
    net_score: int
    user_vote: Optional[int] = None


class RefreshActionResponse(BaseModel):
    """Response from POST /forecasts/{id}/refresh."""

    updated: bool
    reason: Optional[str] = None
    update: Optional[ForecastUpdateRecord] = None
