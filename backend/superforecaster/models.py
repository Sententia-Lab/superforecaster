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

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_SEARCH_DEPTH = 50
"""Ceiling on a retried step's `max_iterations` override, and the one number the UI
must not guess.

A budget overrun is the usual reason to retry a step, and the error text tells the
reader to come back with a higher depth — so this has to be high enough to accept what
that invites. It was 20 while the resume prompt suggested doubling, which turned a
reasonable "try 25" into a 422 the UI rendered as `[object Object]`.
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
        description="Stable key ('sq1'). Assigned by `run_decompose`, not the model — "
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


# ---------- Graph step outputs ----------


ChainRule = Literal["conjunction", "disjunction", "custom"]

DependenceKind = Literal["none", "shared_driver", "one_causes_other"]

DEPENDENCE: dict[str, float] = {
    "none": 0.0,
    "shared_driver": 0.35,
    "one_causes_other": 0.50,
}
"""Kind of link -> dependence parameter: how far a group's joint probability sits from
independence toward the Fréchet-Hoeffding upper bound. ADR 65.

Two marginals never determine their joint — they leave one free number, and multiplying
them picks one point in its range by assumption. This names that assumption.

The three values are chosen priors, not measurements. Storing the kind rather than the
number is what makes them fittable later: every forecast that used `shared_driver`
contributes to one estimate.

Here rather than in `config.CheckThresholds` because it is the meaning of the label, not
an operator setting — `shared_driver` *is* 0.35. `checks.py` promises in its own docstring
to hold no numeric literals, and this keeps that true.
"""


class DependentGroup(BaseModel):
    """Sub-questions that do not move independently.

    `members` are 1-based positions in `Decomposition.sub_questions`, not `sq` ids. The
    decompose agent never sees ids — `with_ids` stamps them after the agent returns — and
    an edit re-stamps them by position anyway, so position is the stable thing.
    """

    name: str = Field(description="What the members share, in a few words")
    members: list[int] = Field(
        min_length=2,
        description="1-based positions in `sub_questions`, in the order listed",
    )
    kind: DependenceKind = "none"


class Decomposition(BaseModel):
    """Output of decompose agent. P1.

    Reuses `SubPrediction` rather than defining a parallel type — the decomposition
    the agent produces is the same decomposition that gets persisted on the Forecast.
    """

    sub_questions: list[SubPrediction] = Field(min_length=3, max_length=5)
    chain_rule: ChainRule = Field(
        default="custom",
        description="conjunction = every sub-question must hold, so the rates multiply; "
        "disjunction = any one suffices, so 1 - prod(1 - p); custom = neither, and "
        "`chain_note` has to say what the relationship actually is.",
    )
    """How the sub-questions combine, as arithmetic rather than prose.

    `chain_note` has always asked for this distinction — "multiply for a conjunction,
    take the maximum for alternatives, and say which it is" — and nothing could read the
    answer. This makes it a field, so `checks.combine_sub_question_rates` can apply it and
    the anchor becomes the chain the decomposition describes rather than an average of
    lenses pointed at different questions.

    Defaults to `custom` so a checkpoint written before this field existed still loads on
    resume (ADR 28), the same reason `SubPrediction.knowability` defaults to `judgment`.
    """

    dependent_groups: list[DependentGroup] = Field(
        default_factory=list,
        description="Sets of sub-questions that move together. Each set combines under "
        "its own dependence parameter first; the set values and the ungrouped rates then "
        "combine independently. Empty when every sub-question stands on its own.",
    )
    """Which sub-questions are not independent, and how strongly. P1.

    Defaults to empty so a checkpoint written before this field existed still loads on
    resume (ADR 28), the same reason `chain_rule` defaults to `custom`. Empty is a
    dependence parameter of 0 everywhere, which is the arithmetic this had before.
    """

    chain_note: str = Field(
        description="How the sub-questions combine into the whole question"
    )

    @model_validator(mode="after")
    def _groups_name_real_sub_questions(self) -> "Decomposition":
        """A group has to be applicable, and the groups have to partition.

        Raises rather than dropping. The decompose agent retries with the message
        attached, and a hand edit gets a 422 naming the problem — both better than a
        silently discarded field the reader still sees on screen.
        """
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
    """One block of cases behind a base rate. Counted or published, never both.

    A base rate used to be a number the model asserted. It is now `Σ hits / Σ n` over
    these blocks, which is the difference between "70%" and "7 of the 10 cases I found
    did, and here they are".

    Two kinds, because both are legitimate and they are audited differently. `counted` is
    cases the agent enumerated — `check_base_rate_derivation` matches its numbers against
    the named analogs. `published` is a statistic someone else measured, which is how you
    get an n of 230 that nobody could list; it is audited by requiring the source to be
    one the tools actually retrieved.
    """

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
    """One reference population, named before its rate is known. P4 + P7.

    Two lenses on one sub-question is P7's dragonfly eye: a single reference class is a
    single lens and will mislead you. They are chosen in their own step, before any rate
    has been seen, so the choice of population cannot be quietly fitted to the answer it
    produces.

    `weight` is the **only** number in the whole pipeline that no check can verify.
    Everything else — the base rate, the adjusted rate, the sub-question rate, the final
    probability — is derived from evidence and re-derivable. So this one carries a
    mandatory rationale, and the UI shows what each lens alone implied.
    """

    name: str = Field(description="Short label for this population")
    population: str = Field(
        description="Who is in it, precisely enough that someone else could count the "
        "same cases. 'Large tech companies' is not countable; 'US-listed software "
        "companies with >$10B revenue that filed an S-1 between 2015 and 2025' is."
    )
    why_it_fits: str = Field(
        description="Why cases from this population tell you about THIS sub-question"
    )
    weight: float = Field(
        gt=0.0,
        le=1.0,
        description="How well this population resembles this case, relative to the "
        "other lenses. Relevance ONLY — never sample size. A well-fitting population "
        "measured over 12 cases should outweigh a poorly-fitting one measured over 200.",
    )
    weight_rationale: str = Field(
        description="Argue for the weight. This is the one judgment in the pipeline "
        "nothing else can check, so it has to justify itself."
    )


class ResearchedLens(Lens):
    """A lens once its population has been measured.

    No `base_rate` field. The rate is `checks.lens_rate` over `evidence` — computed, not
    asserted, so the number and the cases behind it cannot tell different stories.
    """

    evidence: list[Evidence] = Field(
        min_length=1,
        description="At least one block. A rate you reasoned your way to "
        "is not a base rate.",
    )
    analogs: list[HistoricalAnalog] = Field(
        default_factory=list,
        description="The named cases behind the `counted` evidence. One per case counted "
        "— these are what make the count auditable rather than a claim.",
    )
    sub_question_ids: list[str] = Field(
        default_factory=list,
        description="Which sub-question this lens informs. Stamped by code, not by you.",
    )


class SubQuestionLenses(BaseModel):
    """The `choose_lenses` step's answer for one sub-question. No rates yet."""

    lenses: list[Lens] = Field(min_length=1, max_length=3)


class SubQuestionLensesEdit(SubQuestionLenses):
    """A lens set a person wrote. `SubQuestionLenses` already caps the count at 1-3.

    The agent's own output is rescaled to sum to 1 by `stages.normalize_weights`, but a
    hand-written set is rejected instead. Silently rewriting numbers somebody typed would
    hide the constraint rather than teach it.
    """

    @model_validator(mode="after")
    def _one_whole_judgment(self) -> "SubQuestionLensesEdit":
        names = [lens.name for lens in self.lenses]
        if len(set(names)) != len(names):
            # A lens is identified by (sub-question, name) in `run_steps` and in
            # `machine._chosen_lens`. Duplicates are not a style problem — they collide.
            raise ValueError("lens names must be unique within a sub-question")
        total = round(sum(lens.weight for lens in self.lenses), 4)
        if total != 1.0:
            raise ValueError(f"lens weights must sum to 1.00, got {total:.2f}")
        return self


class SubQuestionBaseRates(BaseModel):
    """One researched lens. The `research_lens` step's answer.

    One lens per cell rather than a whole row, because the lens is now the unit the
    research fans out over — five sub-questions with three lenses each is fifteen
    independent searches.
    """

    lens: ResearchedLens
    disagreement: str = Field(
        default="",
        description="What this population might mislead you about, and anything it "
        "already controls for that a later step should not adjust for again.",
    )


class OutsideView(BaseModel):
    """The merge of every researched lens across every sub-question. P4 + P7.

    `aggregate_base_rate` is the anchor before any inside-view move: each sub-question's
    lenses blended by relevance, then combined by the decomposition's chain rule. It is
    computed by `checks.anchor_from`, never asserted.

    `max_length` allows five sub-questions times three lenses each.
    """

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
        description="A short label for this move, six words or fewer. Names the mechanism, "
        "not the direction: 'already cutting subgroup has analogs', not 'raises the "
        "estimate'. `evidence` carries the argument; this is how a reader finds it again.",
    )
    evidence: str
    direction: Direction
    magnitude: float = Field(
        ge=0.0,
        le=0.5,
        description="Probability points to move THIS LENS's base rate, not a multiplier. "
        "0 for noise. A later step blends the adjusted lenses and combines the "
        "sub-questions, so size this against the lens in front of you and nothing else.",
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
    lens_name: str = Field(
        default="",
        description="Which lens this moves. Stamped by code, not volunteered.",
    )
    sub_question_ids: list[str] = Field(
        default_factory=list,
        description="Which sub-question this bears on. Stamped by code. Empty means the "
        "question as a whole — the reflect pass and the whole-question fallback.",
    )


class BiasCheck(BaseModel):
    """P15 — one named bias and how it was countered."""

    bias: BiasName
    assessment: str


class SubQuestionAdjustments(BaseModel):
    """One cell's answer — signed moves for exactly one lens. P5 + P9.

    Scoped to a single lens, not a sub-question, because a modifier is only meaningful
    relative to a population. "Market cap exploded" is already baked into *large-cap tech
    IPOs* and warrants no move; against *all AI labs* it is the whole differentiator.
    Adjusting a blended rate would double-count against the populations that already
    control for the feature.

    No `bias_checks`: three of the five named biases are only askable of a final
    probability, which a lens does not have. Those come from the reflect pass.
    """

    lens_name: str = Field(description="Which lens these move. Stamped by code.")
    adjustments: list[Adjustment] = Field(min_length=1, max_length=3)
    already_controlled_for: str = Field(
        default="",
        description="What this population already accounts for, and therefore what you "
        "deliberately did NOT adjust for. The question that keeps a modifier honest.",
    )
    steel_man: str = Field(
        description="P14 — strongest case against your conclusion for THIS lens"
    )
    what_would_change_my_mind: str = Field(
        description="P14 — for THIS lens specifically"
    )


class Reflection(BaseModel):
    """Output of reflect agent. P14 + P15, over the whole question.

    Runs after the inside-view barrier with every column's adjustments in front of it and
    no tools. That is what makes `check_disconfirming`'s "every adjustment points the same
    direction" evaluable again — no single column can see the others' directions.
    """

    steel_man: str = Field(
        description="P14 — strongest case for the opposite conclusion, whole question"
    )
    what_would_change_my_mind: str = Field(description="P14")
    bias_checks: list[BiasCheck] = Field(min_length=5, max_length=5)


class InsideView(BaseModel):
    """The inside-view row, merged. P5, P9, P14, P15.

    `adjustments` is every cell's, stamped with the lens it came from.
    `max_length` allows fifteen cells — five sub-questions times three lenses —
    times three adjustments each, matching `SubQuestionAdjustments`. The three
    whole-question fields come from `Reflection`.
    """

    adjustments: list[Adjustment] = Field(min_length=1, max_length=45)
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
    """Output of update agent. P10, P11, P12."""

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


# ---------- Resolution agent ----------


class ResolutionCheckResult(BaseModel):
    """Output from resolution agent — binary classification."""

    appears_resolved: bool
    suggested_outcome: Optional[float] = Field(default=None, description="0.0 or 1.0")
    confidence: Confidence
    resolution_evidence: Optional[str] = None
    reasoning: str


# ---------- Criteria critic (P3) ----------


class CriteriaCritique(BaseModel):
    """Output of critic agent. P3.

    Standalone — not part of the forecast graph. The frontend writes both suggestions
    straight into the editor, so every field here is either text to paste or the
    sentence that explains the paste.
    """

    is_resolvable: bool = Field(
        description="False when the criteria could not be adjudicated as written"
    )
    what_changed: str = Field(
        default="",
        description="One or two sentences naming the edits, written for the person "
        "who is about to see them applied. Empty when nothing needed changing.",
    )
    suggested_criteria: str = Field(
        description="Criteria and nothing else — this is pasted over the author's own "
        "text. A question too vague to rewrite returns their text unchanged and asks "
        "for what is missing in `what_changed`."
    )
    suggested_resolution_source: str = Field(
        default="",
        description="REQUIRED in practice: the specific publication, dataset, register "
        "or body that would settle this question. A critique that names none is forced "
        "to `is_resolvable=False` — see `agents.critic._require_a_source`.",
    )


# ---------- Post-mortem (P13) ----------


class PostMortem(BaseModel):
    """Output of postmortem agent. P13.

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


# ---------- Drafting a question from freeform text ----------


class DraftQuestionRequest(BaseModel):
    """Body of POST /questions/draft — one block of text, as the user typed it."""

    text: str = Field(min_length=20)


class DraftedQuestion(BaseModel):
    """Freeform text split into the fields a forecast needs. The `/questions/draft` body.

    All four are filled, so a drafted question is runnable without a second call.
    Whether the criteria are any *good* is still `CriteriaCritique`'s job — keeping the
    two apart is what lets each be scored on one thing.
    """

    question: str = Field(description="The question as a single interrogative sentence")
    resolution_criteria: str
    resolution_date: datetime
    category: str
    resolution_source: str = Field(
        description="Named for every question, whether or not the text named one — the "
        "publication, dataset, register, or body that settles it. The one field the "
        "draft supplies rather than extracts (ADR 64)."
    )


# ---------- Gated runs ----------

GatedRunStatus = Literal["backlog", "active", "complete"]
"""Error is deliberately not a status — it is a nullable field on the run (the red
chip), cleared when the failing step is retried. A run with a failed step is still
`active`; it has not gone anywhere."""

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
    """Everything the final stage produced, persisted as one payload.

    `anchor`, `implied`, and `derivation_slack` are stored as data so the UI can show
    the arithmetic — and the ±slack rule the final probability obeyed — without
    re-deriving thresholds that may have changed since the run.
    """

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
    edited_at: Optional[datetime] = Field(
        default=None,
        description="Set when a person replaced this payload by hand. A payload a human "
        "wrote is different evidence from one the agent produced, so the UI says which.",
    )


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
    """Body of POST /runs. Every field optional — the start gate checks completeness.

    A run missing its question, criteria, date, or source can sit in the backlog
    indefinitely; it just cannot *start*. That is where the four-field rule lives.
    """

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
