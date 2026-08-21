"""Every Pydantic model shared by the agents, the database, and the API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

SourceConfidence = Literal["low", "medium", "high"]
"""How strongly one source supports one claim."""

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
    """One named case behind a counted base rate."""

    description: str = Field(description="Brief description of the analogous event")
    outcome: float = Field(ge=0.0, le=1.0, description="0.0 or 1.0")


class GradedSource(BaseModel):
    """One source and how strongly it supports the claim it is attached to."""

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
        """Drop anything that is not an absolute http(s) URL, such as a redirect fragment."""
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
    """One component of a Fermi-ized decomposition. P1 + P2."""

    id: str = Field(default="", description="Assigned by code, not by you.")
    question: str = Field(description="Specific, testable sub-question")
    probability: float = Field(ge=0.0, le=1.0)
    rationale: str
    knowability: Knowability = Field(
        default="judgment",
        description="researchable = a base rate can be looked up; "
        "judgment = no lookup exists, this needs an estimate",
    )


class ForecastAnswer(BaseModel):
    """What the synthesis agent decides. Code stamps everything else onto `Forecast`."""

    probability: float = Field(ge=0.0, le=1.0)
    reasoning: str
    extreme_justification: str = Field(
        default="",
        description="Required outside the calibration band; empty inside it.",
    )


class Forecast(ForecastAnswer):
    """A published forecast: the answer plus the question and decomposition it came from."""

    question: str
    resolution_criteria: str
    resolution_date: datetime
    category: str
    decompositions: list[SubPrediction] = Field(min_length=3, max_length=5)


class ForecastInput(BaseModel):
    """The question a run forecasts."""

    question: str
    resolution_criteria: str
    resolution_date: datetime
    category: str
    max_iterations: int = 5


# ---------- Graph step outputs ----------


ChainRule = Literal["conjunction", "disjunction", "custom"]

DependenceKind = Literal["none", "shared_driver", "one_causes_other"]

DEPENDENCE: dict[str, float] = {
    "none": 0.0,
    "shared_driver": 0.35,
    "one_causes_other": 0.50,
}
"""Dependence kind -> how far a group's joint probability sits from independence toward
the Fréchet-Hoeffding bound. Chosen priors, not measurements (ADR 65)."""


class DependentGroup(BaseModel):
    """Sub-questions that do not move independently, named by 1-based position."""

    name: str = Field(description="What the members share, in a few words")
    members: list[int] = Field(
        min_length=2,
        description="1-based positions in `sub_questions`, in the order listed",
    )
    kind: DependenceKind = "none"


class Decomposition(BaseModel):
    """Output of the decompose agent. P1."""

    sub_questions: list[SubPrediction] = Field(min_length=3, max_length=5)
    chain_rule: ChainRule = Field(
        default="custom",
        description="conjunction = every sub-question must hold, so the rates multiply; "
        "disjunction = any one suffices, so 1 - prod(1 - p); custom = neither, and "
        "`chain_note` has to say what the relationship actually is.",
    )
    dependent_groups: list[DependentGroup] = Field(
        default_factory=list,
        description="Sets of sub-questions that move together; empty when independent.",
    )

    chain_note: str = Field(
        description="How the sub-questions combine into the whole question"
    )

    @model_validator(mode="after")
    def _groups_name_real_sub_questions(self) -> "Decomposition":
        """Every group names real positions, each position is in at most one group."""
        if self.dependent_groups and self.chain_rule == "custom":
            raise ValueError(
                "a custom chain rule has no formula for dependence to move"
            )
        seen: set[int] = set()
        for g in self.dependent_groups:
            for m in g.members:
                if not 1 <= m <= len(self.sub_questions):
                    raise ValueError(
                        f"group '{g.name}' names sub-question {m} of "
                        f"{len(self.sub_questions)}"
                    )
                if m in seen:
                    raise ValueError(f"sub-question {m} is in more than one group")
                seen.add(m)
        return self


class Evidence(BaseModel):
    """One block of cases behind a base rate: `counted` (cases the agent listed) or
    `published` (a statistic someone else measured, which needs a source)."""

    kind: Literal["counted", "published"]
    hits: int = Field(ge=0, description="Cases in this block where the thing happened")
    n: int = Field(ge=1, description="Cases in this block considered at all")
    note: str = Field(
        description="counted: what was enumerated. published: the statistic and the "
        "population it was measured over."
    )
    source: GradedSource | None = Field(
        default=None,
        description="Required for `published`. A statistic with no source "
        "is an assertion.",
    )

    @model_validator(mode="after")
    def _coherent(self) -> "Evidence":
        if self.hits > self.n:
            raise ValueError(f"hits ({self.hits}) cannot exceed n ({self.n})")
        if self.kind == "published" and self.source is None:
            raise ValueError("published evidence needs a source")
        return self


class Lens(BaseModel):
    """One reference population, named before its rate is known. P4 + P7."""

    name: str = Field(description="Short label for this population")
    population: str = Field(
        description="Who is in it, precisely enough that someone else could count the same cases"
    )
    why_it_fits: str = Field(
        description="Why this population informs THIS sub-question"
    )
    weight: float = Field(
        gt=0.0,
        le=1.0,
        description="Fit relative to the other lenses. Never sample size.",
    )
    weight_rationale: str = Field(description="Argue for the weight.")


class ResearchedLens(Lens):
    """A lens once measured. The rate is `checks.lens_rate` over `evidence`."""

    evidence: list[Evidence] = Field(min_length=1)
    analogs: list[HistoricalAnalog] = Field(default_factory=list)
    sub_question_ids: list[str] = Field(default_factory=list)


class SubQuestionLenses(BaseModel):
    """The `choose_lenses` step's answer for one sub-question. No rates yet."""

    lenses: list[Lens] = Field(min_length=1, max_length=3)


class SubQuestionLensesEdit(SubQuestionLenses):
    """A lens set a person wrote. Rejected rather than rescaled (ADR 54)."""

    @model_validator(mode="after")
    def _one_whole_judgment(self) -> "SubQuestionLensesEdit":
        names = [lens.name for lens in self.lenses]
        if len(set(names)) != len(names):
            raise ValueError("lens names must be unique within a sub-question")
        total = round(sum(lens.weight for lens in self.lenses), 4)
        if total != 1.0:
            raise ValueError(f"lens weights must sum to 1.00, got {total:.2f}")
        return self


class BaseRateResult(BaseModel):
    """What one base-rate cell decides. Code attaches it to the chosen lens."""

    evidence: list[Evidence] = Field(
        min_length=1,
        description="At least one block. A reasoned rate is not a base rate.",
    )
    analogs: list[HistoricalAnalog] = Field(
        default_factory=list,
        description="Every case behind the counted evidence, one per case.",
    )
    disagreement: str = Field(
        default="",
        description="What this population might mislead about, and what it already "
        "controls for that a later step should not adjust for again.",
    )


class OutsideView(BaseModel):
    """Every researched lens. `aggregate_base_rate` is computed by `checks.anchor_from`."""

    lenses: list[ResearchedLens] = Field(min_length=1, max_length=15)
    aggregate_base_rate: float = Field(ge=0.0, le=1.0)
    disagreement: str = Field(
        default="",
        description="What the populations might mislead about, per sub-question.",
    )


class Adjustment(BaseModel):
    """One inside-view move away from the base rate. P5 + P9."""

    title: str = Field(
        default="",
        description="Six words or fewer naming the mechanism, not the direction.",
    )
    evidence: str
    direction: Direction
    magnitude: float = Field(
        ge=0.0,
        le=0.5,
        description="Probability points to move this lens's rate. 0 for noise.",
    )
    flip_test: str = Field(
        description="P9 — if the opposite of this evidence were true, my estimate would ___"
    )
    is_noise: bool = Field(
        default=False,
        description="True when the flip test shows the evidence is not decision-relevant",
    )
    sources: list[GradedSource] = Field(
        default_factory=list,
        description="Empty for a judgment call with nothing to look up.",
    )
    lens_name: str = ""
    sub_question_ids: list[str] = Field(default_factory=list)


class BiasCheck(BaseModel):
    """P15 — one named bias and how it was countered."""

    bias: BiasName
    assessment: str


class AdjustmentResult(BaseModel):
    """What one inside-view cell decides, for exactly one lens. P5 + P9 + P14."""

    adjustments: list[Adjustment] = Field(min_length=1, max_length=3)
    steel_man: str = Field(
        description="Strongest case against your conclusion for this lens"
    )


class Reflection(BaseModel):
    """Output of the reflect agent. P14 + P15, over the whole question."""

    steel_man: str = Field(
        description="P14 — strongest case for the opposite conclusion, whole question"
    )
    what_would_change_my_mind: str = Field(description="P14")
    bias_checks: list[BiasCheck] = Field(min_length=5, max_length=5)


class InsideView(Reflection):
    """Every cell's adjustments plus the whole-question reflection."""

    adjustments: list[Adjustment] = Field(min_length=1, max_length=45)


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
    """P11 — likelihood assessment for one new fact."""

    fact: str
    source: str
    p_if_true: float = Field(
        ge=0.0, le=1.0, description="P(seeing this fact | the hypothesis is true)"
    )
    p_if_false: float = Field(
        ge=0.0, le=1.0, description="P(seeing this fact | the hypothesis is false)"
    )


class UpdateDecision(BaseModel):
    """Output of update agent. P10, P11, P12."""

    evidence: list[EvidenceItem] = Field(default_factory=list)
    prior: float = Field(ge=0.0, le=1.0)
    posterior: float = Field(ge=0.0, le=1.0)
    reasoning: str
    verified_large_move: bool = Field(
        default=False, description="Set by the VerifyLargeMove node, not by the model"
    )


class UpdateOutcome(BaseModel):
    """Final output of the update cycle, and everything a caller needs to persist it."""

    flagged_resolved: bool = False
    updated: bool = False
    new_probability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    violations: list[CheckViolation] = Field(default_factory=list)
    reason: str = ""
    reasoning: str = ""


# ---------- Resolution agent ----------


class ResolutionCheckResult(BaseModel):
    """Output from resolution agent — binary classification."""

    appears_resolved: bool
    suggested_outcome: Optional[float] = Field(default=None, description="0.0 or 1.0")
    confidence: SourceConfidence
    resolution_evidence: Optional[str] = None
    reasoning: str


# ---------- Criteria critic (P3) ----------


class CriteriaCritique(BaseModel):
    """Output of the critic agent. P3. Both suggestions are pasted into the editor."""

    is_resolvable: bool
    what_changed: str = Field(
        default="", description="One or two sentences naming the edits; empty if none."
    )
    suggested_criteria: str = Field(
        description="Criteria text only; pasted over the author's"
    )
    suggested_resolution_source: str = Field(
        default="", description="The publication, dataset, or body that settles this."
    )


# ---------- Post-mortem (P13) ----------


class PostMortem(BaseModel):
    """Output of the postmortem agent. P13."""

    process_errors: list[str] = Field(default_factory=list)
    outcome_noise: list[str] = Field(default_factory=list)
    verdict: Literal["sound_process", "flawed_process", "insufficient_evidence"]
    lesson: str


# ---------- Sources ----------


class SourceRef(BaseModel):
    """One URL a tool returned. `check_citations` rejects a citation absent from here."""

    url: str
    title: str = ""
    query: str = ""
    published_date: Optional[datetime] = None
    tool: str


class ResearchDoc(BaseModel):
    """One page the run fetched, kept whole in the research store."""

    url: str
    title: str = ""
    body: str = ""


class ResearchHit(BaseModel):
    """One document the research store returned. `score` is None on a browse."""

    rank: int
    url: str
    title: str
    content: str
    score: Optional[float] = None
    marked_url: Optional[str] = None
    """The URL with search hits marked, for the research panel."""


class ResearchPage(BaseModel):
    """One page of a run's research store, as the panel reads it."""

    total: int
    query: str = ""
    results: list[ResearchHit] = []


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
    updates: list[ForecastUpdateRecord]
    research_id: Optional[str] = None
    created_at: datetime


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


# ---------- API responses ----------


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
    resolution_source: Optional[str] = None


# ---------- Drafting a question from freeform text ----------


class DraftQuestionRequest(BaseModel):
    """Body of POST /questions/draft — one block of text, as the user typed it."""

    text: str = Field(min_length=20)


class DraftedQuestion(BaseModel):
    """Freeform text split into the fields a forecast needs."""

    question: str = Field(description="One interrogative sentence")
    resolution_criteria: str
    resolution_date: datetime
    category: str
    resolution_source: str = Field(description="The body that settles the question")


# ---------- Gated runs ----------

GatedRunStatus = Literal["backlog", "active", "complete"]
"""Error is not a status but a nullable field on the run, cleared on retry."""

StepStatus = Literal["pending", "running", "complete", "error"]

Stage = Literal["decompose", "lenses", "base_rates", "inside_view", "synthesis"]


class BaseRateStepPayload(BaseModel):
    """What one base-rate cell persists: the measured lens plus its audit trail."""

    lens: ResearchedLens
    disagreement: str = ""
    sources: list[SourceRef] = Field(default_factory=list)


class InsideStepPayload(BaseModel):
    """What one inside-view cell persists: stamped adjustments plus audit trail."""

    lens_name: str = ""
    adjustments: list[Adjustment] = Field(default_factory=list)
    steel_man: str = ""
    sources: list[SourceRef] = Field(default_factory=list)


class SynthesisStepPayload(BaseModel):
    """Everything the final stage produced. `anchor`, `implied`, and `derivation_slack`
    are stored so the UI can show the arithmetic as it was at the time."""

    reflection: Reflection
    outside: OutsideView
    inside: InsideView
    forecast: Forecast
    violations: list[CheckViolation] = Field(default_factory=list)
    anchor: float
    implied: float
    derivation_slack: float
    attempts: int = 1


class RunStepOut(BaseModel):
    """One gated step as the API returns it."""

    id: str
    run_id: str
    stage: Stage
    sub_question_id: str = ""
    lens_name: str = ""
    status: StepStatus
    payload: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    attempts: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    edited_at: Optional[datetime] = None
    """Set when a person replaced this payload by hand."""


class GatedRunSummary(BaseModel):
    """A run as the sidebar shows it — no steps, just counts."""

    id: str
    question: str
    status: GatedRunStatus
    error: Optional[str] = None
    forecast_id: Optional[str] = None
    stage_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class GatedRunDetail(GatedRunSummary):
    """The full tree — the reload path. The UI rebuilds the whole view from this."""

    resolution_criteria: str = ""
    resolution_source: str = ""
    resolution_date: Optional[datetime] = None
    category: str = "general"
    max_iterations: int = 5
    steps: list[RunStepOut] = Field(default_factory=list)


class CreateGatedRunRequest(BaseModel):
    """Body of POST /runs. Every field optional; the start gate checks completeness."""

    question: str = ""
    resolution_criteria: str = ""
    resolution_source: str = ""
    resolution_date: Optional[datetime] = None
    category: str = "general"
    max_iterations: int = Field(default=5, ge=1, le=20)


class UpdateGatedRunRequest(BaseModel):
    """Body of PATCH /runs/{id} — backlog edits only."""

    question: Optional[str] = None
    resolution_criteria: Optional[str] = None
    resolution_source: Optional[str] = None
    resolution_date: Optional[datetime] = None
    category: Optional[str] = None
    max_iterations: Optional[int] = Field(default=None, ge=1, le=20)
