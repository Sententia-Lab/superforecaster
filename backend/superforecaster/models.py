"""All Pydantic models shared across agents, db, and api.

Models are grouped by concern:
- Forecast inputs / agent outputs
- Graph step outputs (decompose / outside view / inside view)
- Methodology checks
- Refresh + Resolution agent outputs
- DB record types (rows + computed)
- API request / response shapes
- Model garden + evals
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

MAX_SEARCH_DEPTH = 50
"""Ceiling on `max_iterations`, and the one number the UI must not guess.

A budget overrun is the usual reason to resume, and the error text tells the reader to
come back with a higher depth — so this has to be high enough to accept what that
invites. It was 20 while the resume prompt suggested doubling, which turned a reasonable
"try 25" into a 422 the UI rendered as `[object Object]`.
"""

SourceConfidence = Literal["low", "medium", "high"]
"""How strongly one source supports one claim. See `GradedSource`.

This is the only confidence concept on the forecast path. A forecast-level label used
to exist and was removed: it was self-reported, nothing verified it, and
`check_calibration_hygiene` gated on it — so a retry could pass by relabelling rather
than by finding better evidence.
"""

Confidence = Literal["low", "medium", "high"]
"""The resolution checker's certainty that a question *has resolved*.

A different axis from `SourceConfidence` — it grades an observation about the world,
not the support behind a claim. Kept separate so the two cannot drift into each other.
"""

QuestionStatus = Literal["pending", "approved", "rejected", "forecasted"]
Knowability = Literal["researchable", "judgment"]
Direction = Literal["up", "down", "neutral"]
BiasName = Literal[
    "confirmation",
    "availability",
    "narrative",
    "scope_insensitivity",
    "anchoring",
]
ALL_BIASES: tuple[BiasName, ...] = (
    "confirmation",
    "availability",
    "narrative",
    "scope_insensitivity",
    "anchoring",
)


# ---------- Forecast agent ----------


class HistoricalAnalog(BaseModel):
    """An analogous historical event used to build the empirical base rate."""

    description: str = Field(description="Brief description of the analogous event")
    outcome: float = Field(ge=0.0, le=1.0, description="0.0 or 1.0")
    relevance: str = Field(
        description="Why this analog applies to the current question"
    )


class GradedSource(BaseModel):
    """One source, and how strongly it supports the claim it is attached to.

    The grade belongs to the *edge* between a source and a claim, not to either alone:
    a strong dataset can be weak support for a question it only glances at. `note` is
    where that judgment gets stated, since nothing downstream can recompute it.

    `url` is optional because not every source has one (a paywalled dataset, a figure
    quoted in a filing). When present, `checks.check_citations` verifies the agent
    actually fetched it.
    """

    source: str = Field(
        description="Human label for the source — the publication, dataset, or filing. "
        "A title, not a URL."
    )
    url: Optional[str] = Field(
        default=None, description="Only if the agent actually retrieved this URL"
    )
    confidence: SourceConfidence = Field(
        description="How strongly THIS source supports THIS claim"
    )
    note: str = Field(description="Why that grade — strength, and fit to this claim")

    @field_validator("url")
    @classmethod
    def _absolute_http_url(cls, v: str | None) -> str | None:
        """Drop anything that is not an absolute http(s) URL.

        Search results reach the model as prose, so what comes back here is whatever it
        copied out — sometimes a redirect fragment like `/goto?url=CAES...` rather than a
        link. Rendered as an href that is *relative*, so it resolves against our own
        origin and produces a dead link that looks like a citation.

        Dropped rather than raising: a mangled link is not worth failing a whole forecast
        over, and `check_citations` still has the real URLs in `sources_seen` to judge.
        """
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return v


class SubPrediction(BaseModel):
    """One component of a Fermi-ized decomposition. P1 + P2.

    `knowability` defaults to "judgment" so forecasts persisted before P2 existed
    still deserialize from `decompositions_json`.
    """

    id: str = Field(
        default="",
        description="Stable key ('sc1'). Assigned by `run_decompose`, not the model — "
        "a model-supplied id can duplicate or skip, and reference classes and "
        "adjustments point back at this.",
    )
    question: str = Field(description="Specific, testable sub-question")
    probability: float = Field(ge=0.0, le=1.0)
    rationale: str
    knowability: Knowability = Field(
        default="judgment",
        description="researchable = a base rate can be looked up; "
        "judgment = no lookup exists, this needs an estimate",
    )


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
    decompositions: list[SubPrediction] = Field(min_length=3, max_length=5)
    research: ResearchSummary
    reasoning: str
    extreme_justification: str = Field(
        default="",
        description="P16 — required when the probability sits outside the calibration "
        "band. Which reference class carries the extreme, why the spread does not "
        "undercut it, and what would have to be true for it to be wrong. Empty inside "
        "the band.",
    )


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


# ---------- Graph step outputs ----------


class Decomposition(BaseModel):
    """Output of decompose_agent. P1.

    Reuses `SubPrediction` rather than defining a parallel type — the decomposition
    the agent produces is the same decomposition that gets persisted on the Forecast.
    """

    sub_claims: list[SubPrediction] = Field(min_length=3, max_length=5)
    chain_note: str = Field(
        description="How the sub-claims combine into the whole question"
    )


class ReferenceClass(BaseModel):
    """One outside-view lens. P4 + P7.

    `weight` is recorded rather than left implicit because `aggregate_base_rate` is a
    weighted blend the agent used to perform in its head — which made the anchor of the
    whole P6 chain unverifiable. With weights on the record, `checks.check_aggregation`
    can confirm the anchor is what the classes actually say.
    """

    name: str = Field(description="What population this rate is drawn from")
    base_rate: float = Field(ge=0.0, le=1.0)
    sample_size: int = Field(
        ge=1, description="How many analogous cases back this rate"
    )
    weight: float = Field(
        ge=0.0,
        le=1.0,
        description="How well this class fits THIS question, relative to the others. "
        "Not sample size — a large but ill-fitting class should weigh less.",
    )
    sources: list[GradedSource] = Field(
        min_length=1, description="A rate you reasoned your way to is not a base rate"
    )
    sub_claim_ids: list[str] = Field(
        default_factory=list,
        description="Which decomposed sub-claims this class informs. Empty means the "
        "question as a whole.",
    )
    analogs: list[HistoricalAnalog] = Field(default_factory=list)


class OutsideView(BaseModel):
    """Output of outside_view_agent. P4 + P7.

    `min_length=2` on `reference_classes` is the half of P7 (dragonfly eye) that a
    schema can enforce. The other half — that disagreement between lenses gets
    explained rather than silently averaged away — is `checks.check_dragonfly`.
    """

    reference_classes: list[ReferenceClass] = Field(min_length=2, max_length=5)
    aggregate_base_rate: float = Field(ge=0.0, le=1.0)
    disagreement: str = Field(
        default="",
        description="Why the reference classes disagree, and what that implies for "
        "uncertainty. Empty only when they broadly agree.",
    )


class Adjustment(BaseModel):
    """One inside-view move away from the base rate. P5 + P9."""

    evidence: str
    direction: Direction
    magnitude: float = Field(
        ge=0.0,
        le=0.5,
        description="Probability points to move, not a multiplier. 0 for noise.",
    )
    flip_test: str = Field(
        description="P9 — if the opposite of this evidence were true, my estimate would ___"
    )
    is_noise: bool = Field(
        default=False,
        description="True when the flip test shows this evidence is not decision-relevant",
    )
    sources: list[GradedSource] = Field(
        default_factory=list,
        description="May be empty — a judgment call with no lookup behind it. That is "
        "an honest signal, and it grades as low support rather than none.",
    )
    sub_claim_ids: list[str] = Field(
        default_factory=list,
        description="Which decomposed sub-claims this adjustment bears on. Empty means "
        "the question as a whole.",
    )


class BiasCheck(BaseModel):
    """P15 — one named bias and how it was countered."""

    bias: BiasName
    assessment: str


class InsideView(BaseModel):
    """Output of inside_view_agent. P5, P9, P14, P15."""

    adjustments: list[Adjustment] = Field(min_length=1, max_length=8)
    steel_man: str = Field(
        description="P14 — strongest case for the opposite conclusion"
    )
    what_would_change_my_mind: str = Field(description="P14")
    bias_checks: list[BiasCheck] = Field(min_length=5, max_length=5)


# ---------- Methodology checks ----------


class CheckViolation(BaseModel):
    """One failed methodology check. Produced by `checks`, never by an LLM."""

    principle: int = Field(
        ge=1, le=16, description="Indexes into spec/superforecasting_methodology.md"
    )
    name: str
    detail: str
    blocking: bool = Field(
        default=True, description="True -> Critique routes back to Synthesize"
    )


# ---------- Refresh agent ----------


class EvidenceItem(BaseModel):
    """P11 — likelihood assessment for one new fact.

    See `checks.check_bayes_direction` for how these two numbers are used.
    """

    fact: str
    source: str
    p_if_true: float = Field(
        ge=0.0, le=1.0, description="P(seeing this fact | the hypothesis is true)"
    )
    p_if_false: float = Field(
        ge=0.0, le=1.0, description="P(seeing this fact | the hypothesis is false)"
    )


class UpdateDecision(BaseModel):
    """Output of update_agent. P10, P11, P12."""

    evidence: list[EvidenceItem] = Field(default_factory=list)
    prior: float = Field(ge=0.0, le=1.0)
    posterior: float = Field(ge=0.0, le=1.0)
    reasoning: str
    verified_large_move: bool = Field(
        default=False, description="Set by the VerifyLargeMove node, not by the model"
    )


class UpdateOutcome(BaseModel):
    """Final output of UpdateGraph."""

    flagged_resolved: bool = False
    updated: bool = False
    new_probability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    violations: list[CheckViolation] = Field(default_factory=list)
    reason: str = ""


class ForecastRefreshResult(BaseModel):
    """Output from refresh_agent — purely about probability updates."""

    should_update: bool
    new_probability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
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


# ---------- Criteria critic (P3) ----------


class CriteriaCritique(BaseModel):
    """Output of critic_agent. P3.

    Standalone — not part of the forecast graph. Powers the frontend's suggestion
    box while a user is drafting a question.
    """

    is_resolvable: bool = Field(
        description="False when the criteria could not be adjudicated as written"
    )
    ambiguities: list[str] = Field(default_factory=list)
    missing: list[str] = Field(
        default_factory=list,
        description="e.g. 'no resolution source', 'no timezone on the date'",
    )
    suggested_criteria: str
    suggested_resolution_source: str = ""


# ---------- Post-mortem (P13) ----------


class PostMortem(BaseModel):
    """Output of postmortem_agent. P13.

    The point is the split: a 70% forecast that resolved "no" is not automatically
    wrong. `process_errors` is what the reasoning got wrong; `outcome_noise` is what
    was genuinely unknowable at forecast time.
    """

    process_errors: list[str] = Field(default_factory=list)
    outcome_noise: list[str] = Field(default_factory=list)
    verdict: Literal["sound_process", "flawed_process", "insufficient_evidence"]
    lesson: str


# ---------- Leakage audit ----------


class SourceRef(BaseModel):
    """One external source an agent saw, recorded so leakage is auditable.

    A backtest run whose `leaked_sources` is non-empty has a bug in the tool clamp —
    the agent read something published after the question was asked.
    """

    url: str
    title: str = ""
    """What the search result called itself. The UI used to fall back to the domain
    because this did not exist, which turned an unparseable URL into 90 characters of
    redirect payload displayed as if it were a headline."""
    query: str = ""
    """The search that returned this result. Makes "which search found this base rate"
    answerable: a claim cites a URL, and the URL knows the query it came from."""
    published_date: Optional[datetime] = None
    tool: str
    as_of: Optional[datetime] = None

    @property
    def is_leak(self) -> bool:
        if self.as_of is None or self.published_date is None:
            return False
        return self.published_date > self.as_of


# ---------- DB record types ----------


class ForecastUpdateRecord(BaseModel):
    """One probability update row."""

    id: str
    forecast_id: str
    probability: float
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


class CritiqueQuestionRequest(BaseModel):
    """Body of POST /questions/critique."""

    question: str
    resolution_criteria: str
    resolution_date: Optional[datetime] = None


# ---------- Drafting a question from freeform text ----------


class DraftQuestionRequest(BaseModel):
    """Body of POST /questions/draft — one block of text, as the user typed it."""

    text: str = Field(min_length=20)


class DraftedQuestion(BaseModel):
    """Freeform text split into the fields a forecast needs.

    Extraction only. Whether the criteria are any good is `CriteriaCritique`'s job —
    keeping them separate is what lets each be scored on one thing.
    """

    question: str = Field(description="The question as a single interrogative sentence")
    resolution_criteria: str
    resolution_date: datetime
    category: str
    resolution_source: str = Field(
        default="", description="Empty when the text never named one"
    )


class DraftResponse(BaseModel):
    """Response from POST /questions/draft — extraction plus its critique."""

    parsed: DraftedQuestion
    critique: CriteriaCritique


# ---------- Live runs ----------

RunStatus = Literal["queued", "running", "done", "error", "cancelled", "lost"]


class RunEvent(BaseModel):
    """One frame on the SSE wire.

    `payload` is an untyped dict deliberately. Fourteen event models would be fourteen
    classes to keep in step with a JavaScript renderer that reads them as JSON
    regardless; `spec/implemented/spec3.1.md` §3.3 is the schema of record.
    """

    seq: int
    run_id: str
    type: str
    stage: str = ""
    attempt: int = 1
    ts: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    """A run as the home rail shows it — no events."""

    id: str
    question: str
    status: RunStatus
    stage: str = ""
    stage_index: int = 0
    attempt: int = 1
    tool_calls: int = 0
    last_seq: int = 0
    forecast_id: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    ended_at: Optional[datetime] = None
    max_iterations: int = 5
    """The depth this run is using. Present so the resume prompt can offer a real
    number — it used to read this field, find nothing, and suggest 10 every time."""


class RunSnapshot(BaseModel):
    """Everything about a run without opening a stream. The polling fallback."""

    summary: RunSummary
    events: list[RunEvent]


class CreateRunRequest(BaseModel):
    """Body of POST /runs."""

    question: str
    resolution_criteria: str
    resolution_date: datetime
    category: str = "general"
    resolution_source: str = ""
    max_iterations: int = Field(default=5, ge=1, le=20)


class ResumeRunRequest(BaseModel):
    """Body of POST /runs/{id}/resume.

    `max_iterations` raises the search budget for the retried node. The usual reason to
    resume is that the old budget ran out, and resuming with the same one would fail at
    the same place.
    """

    max_iterations: Optional[int] = Field(
        default=None,
        ge=1,
        le=MAX_SEARCH_DEPTH,
        description="Search depth for the retried node. The failure that sends people "
        "here is a budget overrun, and the error text invites raising this — so the cap "
        "has to be high enough to accept what it invites.",
    )


# ---------- Model garden ----------


class ModelEntry(BaseModel):
    """One model in the garden.

    `training_cutoff` is what makes a backtest question clean or contaminated. It
    comes from provider documentation — never guessed, and never obtained by asking
    a model about itself, which they are unreliable about.
    """

    id: str = Field(description="Pydantic AI model string, e.g. 'anthropic:claude-...'")
    provider: str
    training_cutoff: date = Field(
        description="Provider's published TRAINING DATA cutoff — the broader range, "
        "not the narrower 'reliable knowledge' cutoff. Stored as the last day of the "
        "stated month; both choices are the conservative direction."
    )
    released: Optional[date] = Field(
        default=None, description="None when the provider does not publish one"
    )
    available: bool = Field(
        default=False, description="Set by `models probe`, not hand-edited"
    )
    notes: str = ""


# ---------- Evals ----------


class GoldenQuestion(BaseModel):
    """One row of evals/golden_questions.json."""

    id: str
    question: str
    resolution_criteria: str
    asked_at: datetime = Field(
        description="Both clamps key off this: tools see nothing published later, "
        "and the model must have a training cutoff earlier than it"
    )
    resolution_date: datetime
    outcome: float = Field(ge=0.0, le=1.0, description="0.0 or 1.0")
    category: str
    baseline_prior: float = Field(
        ge=0.0, le=1.0, description="Human or crowd estimate — the number to beat"
    )
    contamination_risk: int = Field(
        ge=1, le=3, description="1 = obscure, 3 = certainly in training data"
    )


class QuestionScore(BaseModel):
    """One golden question's result in a backtest."""

    id: str
    forecast_probability: Optional[float] = None
    outcome: float
    brier: Optional[float] = None
    baseline_brier: Optional[float] = None
    violations: list[CheckViolation] = Field(default_factory=list)
    model_used: str = ""
    model_cutoff: Optional[date] = Field(
        default=None, description="Proof the run was clean"
    )
    leaked_sources: list[SourceRef] = Field(default_factory=list)
    error: Optional[str] = None
    skipped: Optional[str] = Field(
        default=None,
        description="e.g. 'no available model with a cutoff before 2020-11-04'",
    )

    @property
    def was_scored(self) -> bool:
        return self.brier is not None


class Scorecard(BaseModel):
    """Output of `superforecaster test e2e`."""

    mode: Literal["clean", "production"]
    n: int = 0
    n_scored: int = 0
    n_skipped_no_clean_model: int = 0
    n_error: int = 0
    clean_coverage: float = 0.0
    mean_brier: Optional[float] = None
    baseline_mean_brier: Optional[float] = None
    brier_by_contamination_tier: dict[int, float] = Field(default_factory=dict)
    count_by_contamination_tier: dict[int, int] = Field(default_factory=dict)
    calibration_buckets: list[CalibrationBucket] = Field(default_factory=list)
    process_score: float = 0.0
    round_number_rate: float = 0.0
    leaked_source_count: int = 0
    models_used: dict[str, int] = Field(default_factory=dict)
    violations_by_principle: dict[int, int] = Field(default_factory=dict)
    scores: list[QuestionScore] = Field(default_factory=list)


class ComponentCase(BaseModel):
    """One row of evals/components/<agent>.json.

    `expect` is agent-specific — each scorer in `evals.components.SCORERS` knows how
    to read its own agent's keys — so it stays an untyped dict on purpose.
    """

    id: str
    agent: str
    input: dict[str, Any]
    expect: dict[str, Any] = Field(default_factory=dict)
    as_of: Optional[datetime] = None


class ComponentScore(BaseModel):
    """One component case's result."""

    case_id: str
    passed: bool = False
    assertions: dict[str, bool] = Field(
        default_factory=dict, description="Named assertion -> pass/fail"
    )
    detail: str = ""
    error: Optional[str] = None
    skipped: Optional[str] = None


class ComponentReport(BaseModel):
    """Output of `superforecaster test component <name>`."""

    agent: str
    n: int = 0
    pass_rate: float = 0.0
    assertion_pass_rates: dict[str, float] = Field(default_factory=dict)
    scores: list[ComponentScore] = Field(default_factory=list)
