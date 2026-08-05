"""Methodology checks — pure functions over structured output.

Each principle in `spec/superforecasting_methodology.md` that can be expressed as a
property of the output lives here as a function returning `CheckViolation | None`.
No LLM, no network, no I/O, no imports from `agents` or `graphs`.

Two reasons this is a module and not prompt text:

1. A principle stated in a prompt cannot be tested. A function over a Pydantic model
   can be unit tested in microseconds.
2. The `Critique` node runs these at runtime and routes failures back to the
   synthesis agent with the specific violation attached, so the model is told which
   principle it broke rather than asked to try harder.

Every threshold comes from `config.CheckThresholds`. There are no numeric literals
here beyond mathematical constants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from config import CheckThresholds, get_check_thresholds

from .models import (
    ALL_BIASES,
    Adjustment,
    ChainRule,
    CheckViolation,
    Decomposition,
    Forecast,
    GradedSource,
    InsideView,
    OutsideView,
    ReferenceClass,
    SourceConfidence,
    SourceRef,
    UpdateDecision,
)

# Likelihoods of exactly 0 give an infinite likelihood ratio. Clamp instead.
_LIKELIHOOD_FLOOR = 1e-6

# Below this, a float comparison is treated as "no difference".
_EPSILON = 1e-9


def _thresholds(t: CheckThresholds | None) -> CheckThresholds:
    return t if t is not None else get_check_thresholds()


def signed_adjustment(a: Adjustment) -> float:
    """An adjustment's contribution in probability points. Noise contributes nothing.

    Public because the streaming waterfall chart shows the same anchor -> adjustments
    -> stated walk that `check_derivation` verifies. Re-deriving it there would let the
    picture and the check disagree about what the evidence implies.
    """
    if a.is_noise or a.direction == "neutral":
        return 0.0
    return a.magnitude if a.direction == "up" else -a.magnitude


_signed = signed_adjustment


def implied_probability(o: OutsideView, i: InsideView) -> float:
    """P6. Where the base rate plus the agent's own stated adjustments lands.

    Clamped to [0, 1] — a chain of adjustments can walk off either end, and a forecast
    is a probability regardless of what the arithmetic wanted to say.
    """
    total = o.aggregate_base_rate + sum(signed_adjustment(a) for a in i.adjustments)
    return min(1.0, max(0.0, total))


def base_rate_spread(o: OutsideView) -> float:
    """How far apart the reference classes are: max minus min.

    A range, not a variance — the thresholds it is compared against are calibrated to
    one, and two classes 0.20 apart have a variance of 0.01, an order of magnitude
    smaller. Renaming this without recomputing would change nothing; recomputing it
    without retuning `reference_class_disagreement` would quietly break P7.

    Public because the UI reports it as a statistic in its own right, and re-deriving it
    there would let the number shown and the number checked drift apart.

    Whole-view, so it is only meaningful when every class measures the same thing. Once
    research fans out per sub-claim that stops being true — use `sub_claim_spreads`.
    """
    rates = [rc.base_rate for rc in o.reference_classes]
    return max(rates) - min(rates) if rates else 0.0


_spread = base_rate_spread


def sub_claim_spreads(o: OutsideView) -> dict[str | None, float]:
    """How far apart the lenses are *within each column*: max minus min, per sub-claim.

    Across columns the number means nothing. A 0.15 lens on "will they commit to an IPO"
    and a 0.80 lens on "will they pick an exchange" are not disagreeing — they are
    measuring different questions — but `base_rate_spread` reports 0.65 of disagreement
    and P7 fires on every run.

    Classes naming no sub-claim group under None. After the outside-view merge that can
    only be a hand-built fixture or an artifact from before the fan-out existed, which is
    also why every pre-3.3 fixture forms exactly one group here and behaves as it did.
    """
    by_column: dict[str | None, list[float]] = {}
    for rc in o.reference_classes:
        for key in rc.sub_claim_ids or [None]:
            by_column.setdefault(key, []).append(rc.base_rate)
    return {k: max(v) - min(v) for k, v in by_column.items()}


def worst_sub_claim_spread(o: OutsideView) -> float:
    """The widest within-column disagreement. 0.0 when there is nothing to compare."""
    return max(sub_claim_spreads(o).values(), default=0.0)


def combine_sub_claim_rates(rates: list[float], rule: ChainRule) -> float | None:
    """What the chain the decomposition describes implies, from its parts.

    Public for the same reason as `signed_adjustment` and `weighted_base_rate`:
    `run_outside_view` records the anchor with this and `check_aggregation` re-derives it
    with this, so the recorded number and the check cannot tell different stories.

    None for `custom` — there is no formula to apply, so the caller falls back to the
    weighted mean over all classes, which is what the anchor was before this existed.
    """
    if not rates:
        return None
    if rule == "conjunction":
        return math.prod(rates)
    if rule == "disjunction":
        return 1.0 - math.prod(1.0 - p for p in rates)
    return None


def chain_inputs(d: Decomposition, o: OutsideView) -> list[dict[str, Any]]:
    """Each sub-claim's rate and where it came from, in decomposition order.

    Runs over **every** sub-claim, not just the researched ones. A conjunction over only
    the researched rates silently treats the rest as 1.0:

        prod([0.55, 0.70, 0.60])        = 0.231   sc4 vanishes
        prod([0.55, 0.70, 0.60, 0.80])  = 0.185   sc4 contributes its own estimate

    So a column nothing researched falls back to `SubPrediction.probability` — the
    decompose agent's own working estimate, an existing typed field, marked `estimated`
    so a reader can tell the two apart.
    """
    rows: list[dict[str, Any]] = []
    for s in d.sub_claims:
        researched = sub_claim_rate(s.id, o) if s.id else None
        rows.append(
            {
                "id": s.id,
                "question": s.question,
                "rate": researched if researched is not None else s.probability,
                "source": "researched" if researched is not None else "estimated",
            }
        )
    return rows


_CONFIDENCE_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
_RANK_CONFIDENCE: tuple[SourceConfidence, ...] = ("low", "medium", "high")


def claim_support(sources: list[GradedSource]) -> SourceConfidence:
    """How well one claim is supported: its strongest source.

    Max rather than mean. A claim backed by a solid dataset *and* a blog post is not
    worse supported than one backed by the dataset alone — averaging would penalise
    citing extra corroboration, which teaches the agent to cite less. Overstating is
    caught by `check_citations`, not by arithmetic here.

    No sources at all grades `low` rather than raising: an adjustment can legitimately
    be a judgment call, and recording that honestly is more useful than forbidding it.
    """
    if not sources:
        return "low"
    return _RANK_CONFIDENCE[max(_CONFIDENCE_RANK[s.confidence] for s in sources)]


def _weighted_support(pairs: list[tuple[float, SourceConfidence]]) -> float | None:
    """Weighted mean rank over (weight, grade). None when nothing carries weight."""
    total = sum(w for w, _ in pairs)
    if total <= _EPSILON:
        return None
    return sum(w * _CONFIDENCE_RANK[c] for w, c in pairs) / total


def aggregate_source_confidence(
    o: OutsideView, i: InsideView, t: CheckThresholds | None = None
) -> SourceConfidence:
    """The forecast's overall evidential support — derived, never self-reported.

    Two levels. Within a claim, `claim_support` takes the strongest source. Across
    claims, a weighted mean: reference classes by `weight` (fit, not sample size), and
    adjustments by `magnitude`, since evidence that barely moves the number should not
    drive the grade. Noise is skipped for the same reason `signed_adjustment` zeroes it.

    The two views are normalised separately and then averaged, rather than pooled — a
    class `weight` and an adjustment `magnitude` are different units, and pooling them
    would silently let the outside view dominate as an artefact of scale rather than a
    decision anyone made.
    """
    th = _thresholds(t)
    outside = _weighted_support(
        [(rc.weight, claim_support(rc.sources)) for rc in o.reference_classes]
    )
    inside = _weighted_support(
        [
            (abs(a.magnitude), claim_support(a.sources))
            for a in i.adjustments
            if not a.is_noise
        ]
    )

    scores = [s for s in (outside, inside) if s is not None]
    if not scores:
        return "low"

    mean = sum(scores) / len(scores)
    if mean >= th.support_high:
        return "high"
    if mean >= th.support_medium:
        return "medium"
    return "low"


# ---------- Decomposition ----------


def check_decomposition(
    d: Decomposition, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P1 + P2. Every sub-claim needs a rationale, and not everything can be judgment.

    `knowability` defaults to "judgment", so an all-judgment decomposition catches
    both "the model declined to label" and "the model labeled everything unknowable."
    Either way nothing was researched, which defeats the point of separating the
    knowable from the unknowable.
    """
    if not d.chain_note.strip():
        return CheckViolation(
            principle=1,
            name="decomposition",
            detail="chain_note is empty — the sub-claims were listed but never "
            "combined into the whole question",
        )

    unexplained = [s.question for s in d.sub_claims if not s.rationale.strip()]
    if unexplained:
        return CheckViolation(
            principle=1,
            name="decomposition",
            detail=f"sub-claims with no rationale: {unexplained}",
        )

    if all(s.knowability == "judgment" for s in d.sub_claims):
        return CheckViolation(
            principle=2,
            name="knowability",
            detail="every sub-claim is labeled 'judgment' — nothing was identified as "
            "researchable, so no effort can be directed at lookup-able base rates",
        )

    return None


# ---------- Outside view ----------


def check_dragonfly(
    o: OutsideView, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P7. Disagreement between reference classes is information, not an inconvenience.

    Two lenses saying 12% and 55% is a fact about how uncertain this question is. An
    agent that silently averages them to 33% has thrown that away. The schema already
    requires at least two reference classes (`min_length=2`); this requires that a
    material disagreement between them gets explained.

    Measured **within a column**. Two lenses only disagree if they were looking at the
    same thing, and once research fans out per sub-claim most pairs are not — see
    `sub_claim_spreads`. Fires on the widest column, and names it, so the reader knows
    which part of the question the sentence they are being asked for is about.
    """
    th = _thresholds(t)
    spreads = sub_claim_spreads(o)
    if not spreads or o.disagreement.strip():
        return None

    worst_id, spread = max(spreads.items(), key=lambda kv: kv[1])
    if spread <= th.reference_class_disagreement:
        return None

    rates = ", ".join(
        f"{rc.name}={rc.base_rate:.2f}"
        for rc in o.reference_classes
        if worst_id in (rc.sub_claim_ids or [None])
    )
    where = f"for {worst_id}" if worst_id else "for the question as a whole"
    return CheckViolation(
        principle=7,
        name="dragonfly",
        detail=f"reference classes {where} span {spread:.2f} "
        f"(> {th.reference_class_disagreement:.2f}) but `disagreement` is empty: {rates}",
    )


def weighted_base_rate(o: OutsideView) -> float | None:
    """What the reference classes and their weights imply the anchor should be.

    Public for the same reason as `signed_adjustment`: the UI shows this alongside the
    stated anchor, and re-deriving it there would let the picture and `check_aggregation`
    disagree about what the classes say.

    None when no class carries any weight, which the schema forbids but a hand-built
    fixture can still produce.
    """
    total = sum(rc.weight for rc in o.reference_classes)
    if total <= _EPSILON:
        return None
    return sum(rc.weight * rc.base_rate for rc in o.reference_classes) / total


def classes_for(sub_claim_id: str, o: OutsideView) -> list[ReferenceClass]:
    """The reference classes that say they inform this sub-claim."""
    return [rc for rc in o.reference_classes if sub_claim_id in rc.sub_claim_ids]


def sub_claim_rate(sub_claim_id: str, o: OutsideView) -> float | None:
    """What the outside view actually found for one sub-claim.

    The weighted mean of the classes that name it, by the same `weight` and the same
    arithmetic `check_aggregation` uses on the whole question — so a per-sub-claim rate
    and the overall anchor cannot tell different stories.

    None when no class claims this sub-claim. That is the honest answer for a `judgment`
    sub-claim, and for a `researchable` one it means the research did not land.
    """
    classes = classes_for(sub_claim_id, o)
    total = sum(rc.weight for rc in classes)
    if not classes or total <= _EPSILON:
        return None
    return sum(rc.weight * rc.base_rate for rc in classes) / total


def anchor_from(o: OutsideView, d: Decomposition | None) -> tuple[float | None, str]:
    """The anchor the outside view implies, and which rule produced it.

    With a decomposition carrying a real `chain_rule`, the anchor is that rule applied to
    the per-column rates. Otherwise it is the weighted mean across all classes — the
    pre-3.3 arm, and still the honest answer when there is no chain to apply.

    One function so `run_outside_view` and `check_aggregation` cannot disagree about
    which arm they are in.
    """
    if d is not None and d.chain_rule != "custom":
        rates = [row["rate"] for row in chain_inputs(d, o)]
        combined = combine_sub_claim_rates(rates, d.chain_rule)
        if combined is not None:
            return combined, d.chain_rule
    return weighted_base_rate(o), "weighted mean"


def check_aggregation(
    o: OutsideView,
    d: Decomposition | None = None,
    t: CheckThresholds | None = None,
) -> CheckViolation | None:
    """P7. The single anchor has to be what the columns actually say.

    `aggregate_base_rate` is the anchor of the entire P6 chain. It used to be a weighted
    blend the agent performed in its head — so classes at 0.10 and 0.90 could produce an
    anchor of 0.85 and nothing would object. Then `weight` went on the record, and the
    blend became arithmetic this could re-derive.

    It is now the *chain* the decomposition describes: for a conjunction, the product of
    the per-column rates. A mean of conjunction factors is always ≥ their product, so the
    old arithmetic inflated every conjunctive question's anchor by construction.

    **What this catches now, stated plainly.** `run_outside_view` computes the anchor with
    `anchor_from`, and this re-derives it with `anchor_from`, so no model performs the
    arithmetic and this can no longer catch one performing it badly. It has become a guard
    on the *artifact*: drift between the merge and the rule, a hand-built fixture, a
    checkpoint resumed from an older version. That is a real weakening of what ADR 29
    added, traded for making the failure structurally impossible rather than merely
    checked — the same move ADR 12 made for "outside view first". Written down here so it
    is not later discovered as a tautology nobody chose.

    `d` is optional and second so callers that have no decomposition — the component
    evals, direct unit tests — keep the pre-3.3 behaviour without an edit.
    """
    th = _thresholds(t)
    implied, rule = anchor_from(o, d)
    if implied is None:
        return CheckViolation(
            principle=7,
            name="aggregation",
            detail="no reference class carries any weight, so the aggregate base rate "
            "cannot be derived from them",
        )

    drift = abs(o.aggregate_base_rate - implied)
    if drift > th.aggregate_slack:
        if d is not None and rule != "weighted mean":
            parts = ", ".join(
                f"{row['id']}={row['rate']:.2f}({row['source'][:4]})"
                for row in chain_inputs(d, o)
            )
        else:
            parts = ", ".join(
                f"{rc.name}={rc.base_rate:.2f}@{rc.weight:.2f}"
                for rc in o.reference_classes
            )
        return CheckViolation(
            principle=7,
            name="aggregation",
            detail=f"aggregate_base_rate {o.aggregate_base_rate:.3f} is {drift:.3f} away "
            f"from the {implied:.3f} its own parts imply under `{rule}` "
            f"(slack {th.aggregate_slack:.3f}): {parts}",
        )
    return None


def check_linkage(
    f: Forecast,
    d: Decomposition,
    o: OutsideView,
    i: InsideView,
    t: CheckThresholds | None = None,
) -> CheckViolation | None:
    """P1. A reference back to a sub-claim has to point at one that exists.

    Two ways this dangles. A class or adjustment can name an id the decomposition never
    had; or synthesis, which regenerates `Forecast.decompositions`, can quietly reword
    or drop a sub-claim — leaving every link pointing at nothing. Both are cheap to
    catch and invisible otherwise.
    """
    known = {s.id for s in d.sub_claims if s.id}

    referenced = {
        cid
        for holder in (*o.reference_classes, *i.adjustments)
        for cid in holder.sub_claim_ids
    }
    unknown = sorted(referenced - known)
    if unknown:
        return CheckViolation(
            principle=1,
            name="linkage",
            detail=f"sub-claim id{'s' if len(unknown) != 1 else ''} "
            f"{', '.join(unknown)} referenced but not in the decomposition "
            f"({', '.join(sorted(known)) or 'none'})",
        )

    carried = {s.id for s in f.decompositions if s.id}
    if known and carried != known:
        return CheckViolation(
            principle=1,
            name="linkage",
            detail=f"the forecast carries sub-claims {', '.join(sorted(carried)) or 'none'} "
            f"but the decomposition produced {', '.join(sorted(known))} — every link "
            f"into the dropped ones now points at nothing",
        )
    return None


# ---------- Sources ----------


def _cited_sources(o: OutsideView, i: InsideView) -> list[GradedSource]:
    return [s for rc in o.reference_classes for s in rc.sources] + [
        s for a in i.adjustments for s in a.sources
    ]


def check_citations(
    o: OutsideView,
    i: InsideView,
    seen: Iterable[SourceRef],
) -> CheckViolation | None:
    """Every cited URL has to be one the agent actually fetched.

    A graded source renders as a clickable link, which makes a fabricated URL worse than
    no URL — it looks authoritative. `deps.sources_seen` already records what the tools
    really returned, so this is set membership.

    Takes the retrieved sources as a plain argument rather than reaching for
    `ForecastDeps`: this module does not import from `agents` or `graphs`, and the
    `Critique` node is where the two meet.
    """
    retrieved = {ref.url for ref in seen if ref.url}
    invented = sorted(
        {s.url for s in _cited_sources(o, i) if s.url and s.url not in retrieved}
    )
    if not invented:
        return None

    shown = ", ".join(invented[:3]) + (" …" if len(invented) > 3 else "")
    return CheckViolation(
        principle=4,
        name="citations",
        detail=f"{len(invented)} cited URL{'s' if len(invented) != 1 else ''} "
        f"were never retrieved by a search: {shown}",
    )


# ---------- Inside view ----------


def check_signal_vs_noise(
    i: InsideView, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P9. Every adjustment states what the opposite evidence would have done.

    The flip test is the whole principle: if seeing the opposite would not change the
    estimate, the evidence is noise dressed up as signal. Evidence the agent itself
    marked as noise must therefore move the number by zero.
    """
    missing = [a.evidence for a in i.adjustments if not a.flip_test.strip()]
    if missing:
        return CheckViolation(
            principle=9,
            name="signal_vs_noise",
            detail=f"adjustments with no flip test: {missing}",
        )

    moving_noise = [a.evidence for a in i.adjustments if a.is_noise and a.magnitude > 0]
    if moving_noise:
        return CheckViolation(
            principle=9,
            name="signal_vs_noise",
            detail=f"evidence marked as noise still moved the probability: {moving_noise}",
        )

    return None


def check_disconfirming(
    i: InsideView, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P14. The opposing case is stated, and evidence was sought in both directions.

    The one-sided check only applies with two or more real adjustments — with a single
    piece of evidence there is nothing to be lopsided about, and `steel_man` still
    carries the requirement on its own.
    """
    if not i.steel_man.strip():
        return CheckViolation(
            principle=14,
            name="disconfirming",
            detail="steel_man is empty — the opposing view was never stated",
        )

    if not i.what_would_change_my_mind.strip():
        return CheckViolation(
            principle=14,
            name="disconfirming",
            detail="what_would_change_my_mind is empty — asked after updating, not before",
        )

    real = [a for a in i.adjustments if not a.is_noise and a.direction != "neutral"]
    if len(real) >= 2 and len({a.direction for a in real}) == 1:
        only = real[0].direction
        return CheckViolation(
            principle=14,
            name="disconfirming",
            detail=f"all {len(real)} adjustments point '{only}' — no evidence was found "
            "against the emerging conclusion",
        )

    return None


def check_bias_coverage(
    i: InsideView, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P15. All five named biases are addressed, each with something actually said.

    The schema pins the list to exactly five entries but cannot stop the model naming
    the same bias five times, which is what this catches.
    """
    covered = {b.bias for b in i.bias_checks}
    absent = [b for b in ALL_BIASES if b not in covered]
    if absent:
        return CheckViolation(
            principle=15,
            name="bias_coverage",
            detail=f"biases never addressed: {absent}",
        )

    empty = [b.bias for b in i.bias_checks if not b.assessment.strip()]
    if empty:
        return CheckViolation(
            principle=15,
            name="bias_coverage",
            detail=f"biases named but not assessed: {empty}",
        )

    return None


# ---------- Synthesis ----------


def check_derivation(
    f: Forecast,
    o: OutsideView,
    i: InsideView,
    t: CheckThresholds | None = None,
) -> CheckViolation | None:
    """P6. The final probability follows from the base rate and the stated adjustments.

    This is regression to the mean expressed as arithmetic. The agent has already
    told us where it started and how far each piece of evidence should move it:

        implied = aggregate_base_rate + sum(signed magnitude of non-noise adjustments)

    If the final number is far from `implied`, the agent moved further than its own
    listed evidence supports — which is exactly what abandoning a base rate for a
    compelling narrative looks like from the outside.

    Note this replaces a per-forecast granularity check (P8). A forecast can
    legitimately land on 0.60, and failing it for being a round number would punish a
    correct answer. P8 is a property of a distribution, so it is measured at run level
    by `evals.scoring.round_number_rate` instead.
    """
    th = _thresholds(t)
    implied = implied_probability(o, i)

    drift = abs(f.probability - implied)
    if drift > th.derivation_slack:
        return CheckViolation(
            principle=6,
            name="derivation",
            detail=f"final probability {f.probability:.3f} is {drift:.3f} away from the "
            f"{implied:.3f} implied by base rate {o.aggregate_base_rate:.3f} plus its own "
            f"stated adjustments (slack {th.derivation_slack:.3f})",
        )
    return None


def check_calibration_hygiene(
    f: Forecast,
    o: OutsideView,
    t: CheckThresholds | None = None,
) -> CheckViolation | None:
    """P16. An extreme probability is justified, not forbidden. **Advisory.**

    A well-calibrated 60% beats a miscalibrated 90%. But a hard gate on this is the
    wrong shape, and the old one proved it: it required `confidence == "high"`, a field
    the model wrote itself, so a failing retry could pass by *lowering* confidence to
    "low" and retreating the probability to exactly the floor. Landing on the boundary
    skipped the test entirely, and nothing about the evidence had changed.

    So this no longer blocks. It asks the agent to argue for an extreme in
    `extreme_justification` and flags the ones it did not argue for — the same shape as
    `check_dragonfly` requiring `disagreement`, and the same reasoning as ADR 16: the
    right response to a bold number is to check whether it holds up, not to forbid it.

    Two things get flagged:

    - a probability outside the band with no justification written
    - a probability at the far tail of a wide reference-class spread, where the outside
      view itself is telling you the number is less certain than it looks
    """
    th = _thresholds(t)
    p = f.probability
    justified = bool(f.extreme_justification.strip())
    # Within a column, not across. Post-fan-out the whole-view spread is wide by
    # construction — different columns measure different things — so the second arm below
    # would fire on nearly every run and this advisory would become noise.
    spread = worst_sub_claim_spread(o)

    if p < th.calibration_floor or p > th.calibration_ceiling:
        if justified:
            return None
        return CheckViolation(
            principle=16,
            name="calibration_hygiene",
            detail=f"probability {p:.3f} is outside "
            f"[{th.calibration_floor:.2f}, {th.calibration_ceiling:.2f}] and "
            f"`extreme_justification` is empty — an extreme has to be argued for",
            blocking=False,
        )

    # Inside the band, but hugging an edge while the classes disagree. This is the case
    # the old check waved through: retreating to exactly the floor was enough to pass.
    near_edge = min(p - th.calibration_floor, th.calibration_ceiling - p)
    if (
        near_edge <= th.calibration_floor
        and spread > th.reference_class_agreement
        and not justified
    ):
        return CheckViolation(
            principle=16,
            name="calibration_hygiene",
            detail=f"probability {p:.3f} sits at the edge of "
            f"[{th.calibration_floor:.2f}, {th.calibration_ceiling:.2f}] while the "
            f"reference classes span {spread:.2f} "
            f"(> {th.reference_class_agreement:.2f}) — a near-extreme resting on "
            f"classes that disagree, with nothing written in `extreme_justification`",
            blocking=False,
        )
    return None


# ---------- Updating ----------


def evidence_weight(d: UpdateDecision) -> float:
    """P11. Total weight of evidence, as a sum of log likelihood ratios.

    For each new fact E the agent reports two numbers:

        p_if_true  = P(E | H)      how likely this fact is if the hypothesis is TRUE
        p_if_false = P(E | not H)  how likely this fact is if the hypothesis is FALSE

    Bayes in odds form, where odds(p) = p / (1 - p):

        posterior_odds = prior_odds * (p_if_true / p_if_false)

    That ratio is the likelihood ratio, LR:

        LR > 1   more expected in a true world   -> pushes probability UP
        LR = 1   equally expected either way     -> noise, moves nothing (this is P9)
        LR < 1   more expected in a false world  -> pushes probability DOWN

    Independent evidence multiplies, so n facts compose as a product of LRs. Products
    of many small numbers underflow and are awkward to reason about, so taking logs
    turns the product into a sum:

        log(posterior_odds) = log(prior_odds) + SUM_i log(LR_i)

    This function returns that sum. Its sign is what `check_bayes_direction` uses.
    Evidence with p_if_true == p_if_false contributes log(1) = 0 and drops out, which
    is the correct treatment of a fact that tells you nothing.
    """
    total = 0.0
    for e in d.evidence:
        true_likelihood = max(e.p_if_true, _LIKELIHOOD_FLOOR)
        false_likelihood = max(e.p_if_false, _LIKELIHOOD_FLOOR)
        total += math.log(true_likelihood / false_likelihood)
    return total


def check_bayes_direction(
    d: UpdateDecision, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P11. The probability moved the same way the agent's own likelihoods point.

    This checks the sign, not the magnitude. Requiring the exact Bayesian posterior
    would be too strict — the methodology is explicit that the discipline of asking
    the question provides most of the value and that formal Bayes is not required.

    But an agent that says "this evidence makes the outcome more likely" and then
    lowers its probability has contradicted itself, and that is always an error:

        SUM log(LR) > 0   net-confirming     -> posterior must be > prior
        SUM log(LR) < 0   net-disconfirming  -> posterior must be < prior
        SUM log(LR) = 0   net-neutral        -> posterior must not move materially

    The net-neutral arm also catches a move made with no evidence cited at all, since
    an empty evidence list sums to zero.
    """
    th = _thresholds(t)
    weight = evidence_weight(d)
    move = d.posterior - d.prior

    if weight > _EPSILON and move < -_EPSILON:
        return CheckViolation(
            principle=11,
            name="bayes_direction",
            detail=f"evidence is net-confirming (weight {weight:+.3f}) but the "
            f"probability fell {d.prior:.3f} -> {d.posterior:.3f}",
        )

    if weight < -_EPSILON and move > _EPSILON:
        return CheckViolation(
            principle=11,
            name="bayes_direction",
            detail=f"evidence is net-disconfirming (weight {weight:+.3f}) but the "
            f"probability rose {d.prior:.3f} -> {d.posterior:.3f}",
        )

    if abs(weight) <= _EPSILON and abs(move) >= th.min_probability_delta:
        return CheckViolation(
            principle=11,
            name="bayes_direction",
            detail=f"evidence carries no net weight but the probability moved "
            f"{d.prior:.3f} -> {d.posterior:.3f} "
            f"(>= {th.min_probability_delta:.3f})",
        )

    return None


def check_update_magnitude(
    d: UpdateDecision, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P10 + P12. Under-reaction only — anchoring on the prior despite real evidence.

    This deliberately does not fail large moves. Genuinely decisive news exists: FTX
    filing for bankruptcy, the FDIC seizing SVB. A hard cap would reject the correct
    behaviour. Large moves route through the `VerifyLargeMove` graph node instead,
    which makes the agent corroborate the claim rather than forbidding the move.
    """
    weight = evidence_weight(d)
    move = abs(d.posterior - d.prior)

    if abs(weight) > _EPSILON and move <= _EPSILON:
        return CheckViolation(
            principle=12,
            name="under_reaction",
            detail=f"evidence carries weight {weight:+.3f} but the probability did not "
            f"move from {d.prior:.3f} — anchoring on the prior",
        )

    return None


def is_large_move(d: UpdateDecision, t: CheckThresholds | None = None) -> bool:
    """P12. Whether this update is big enough to demand a verification pass.

    A routing signal for the `GuardUpdate` node, not a violation.
    """
    th = _thresholds(t)
    return abs(d.posterior - d.prior) > th.large_move


# ---------- Suites ----------


@dataclass(frozen=True)
class CheckResult:
    """One check, whether it passed, and the data it looked at.

    Exists because a UI showing the critique needs to distinguish "this check ran and
    passed" from "this check never ran" — and a list of violations cannot. The label is
    carried here rather than derived by the caller so the principle numbering has one
    home.

    `evidence` is the input the verdict was reached on: the actual base rates and
    spread for P7, the actual anchor-plus-adjustments walk for P6, the five bias slots
    and which were filled for P15. A violation's `detail` says what went wrong in a
    sentence; this is the material to check that sentence against.
    """

    principle: int
    name: str
    label: str
    passed: bool
    violation: CheckViolation | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


FORECAST_CHECK_LABELS: tuple[tuple[str, int, str], ...] = (
    ("decomposition", 1, "P1 · P2 decomposition"),
    ("linkage", 1, "P1 sub-claim linkage"),
    ("dragonfly", 7, "P7 dragonfly"),
    ("aggregation", 7, "P7 base-rate aggregation"),
    ("citations", 4, "P4 citations"),
    ("signal_vs_noise", 9, "P9 signal vs noise"),
    ("disconfirming", 14, "P14 disconfirming"),
    ("bias_coverage", 15, "P15 bias coverage"),
    ("derivation", 6, "P6 derivation"),
    ("calibration_hygiene", 16, "P16 calibration hygiene"),
)
"""(name, principle, display label) in the order the checks run and are shown.

The order matches `run_forecast_checks_detailed`. A check that fails may report a
different `name` than the slot it occupies — `check_decomposition` returns
"knowability" for its P2 arm — so the slot name is what identifies the row, and the
violation carries its own more specific name.
"""


def check_evidence(
    forecast: Forecast,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    t: CheckThresholds | None = None,
) -> dict[str, dict[str, Any]]:
    """The material each check reasoned over, keyed by check name.

    Built here rather than inside the seven validators, so those stay exactly as they
    were: they answer pass or fail, and this answers "on what basis". A violation's
    `detail` states the conclusion in a sentence; this is what you check that sentence
    against. Pure, like everything else in this module.
    """
    th = _thresholds(t)

    walk: list[dict[str, Any]] = []
    running = outside.aggregate_base_rate
    for a in inside.adjustments:
        delta = signed_adjustment(a)
        running = min(1.0, max(0.0, running + delta))
        walk.append(
            {
                "evidence": a.evidence,
                "direction": a.direction,
                "delta": delta,
                "running": running,
                "is_noise": a.is_noise,
            }
        )
    implied = implied_probability(outside, inside)
    covered = {b.bias for b in inside.bias_checks}
    real = [a for a in inside.adjustments if not a.is_noise and a.direction != "neutral"]

    return {
        "decomposition": {
            "chain_note": decomposition.chain_note,
            "sub_claims": [
                {
                    "question": s.question,
                    "probability": s.probability,
                    "knowability": s.knowability,
                    "has_rationale": bool(s.rationale.strip()),
                }
                for s in decomposition.sub_claims
            ],
            "researchable": sum(
                1 for s in decomposition.sub_claims if s.knowability == "researchable"
            ),
        },
        "dragonfly": {
            "classes": [
                {
                    "name": rc.name,
                    "base_rate": rc.base_rate,
                    "sample_size": rc.sample_size,
                    "weight": rc.weight,
                    "support": claim_support(rc.sources),
                    "sub_claim_ids": rc.sub_claim_ids,
                }
                for rc in outside.reference_classes
            ],
            # Both: `spreads` is what the check actually reads, `spread` the worst of
            # them. The whole-view number is kept because a reader comparing two columns
            # wants to know it is not the thing being judged.
            "spreads": {k or "": v for k, v in sub_claim_spreads(outside).items()},
            "spread": worst_sub_claim_spread(outside),
            "whole_view_spread": base_rate_spread(outside),
            "threshold": th.reference_class_disagreement,
            "disagreement": outside.disagreement,
        },
        "linkage": {
            "sub_claims": [
                {"id": s.id, "question": s.question} for s in decomposition.sub_claims
            ],
            "classes": [
                {"name": rc.name, "sub_claim_ids": rc.sub_claim_ids}
                for rc in outside.reference_classes
            ],
            "adjustments": [
                {"evidence": a.evidence, "sub_claim_ids": a.sub_claim_ids}
                for a in inside.adjustments
            ],
        },
        "aggregation": {
            "classes": [
                {"name": rc.name, "base_rate": rc.base_rate, "weight": rc.weight}
                for rc in outside.reference_classes
            ],
            "stated": outside.aggregate_base_rate,
            "implied": anchor_from(outside, decomposition)[0],
            "rule": anchor_from(outside, decomposition)[1],
            "chain": chain_inputs(decomposition, outside),
            "weighted_mean": weighted_base_rate(outside),
            "slack": th.aggregate_slack,
        },
        "citations": {
            "cited": [
                {"source": s.source, "url": s.url, "confidence": s.confidence}
                for s in _cited_sources(outside, inside)
            ],
            "support": aggregate_source_confidence(outside, inside, th),
        },
        "signal_vs_noise": {
            "adjustments": [
                {
                    "evidence": a.evidence,
                    "magnitude": a.magnitude,
                    "is_noise": a.is_noise,
                    "flip_test": a.flip_test,
                }
                for a in inside.adjustments
            ],
        },
        "disconfirming": {
            "steel_man": inside.steel_man,
            "what_would_change_my_mind": inside.what_would_change_my_mind,
            "directions": [a.direction for a in real],
            "real_adjustments": len(real),
        },
        "bias_coverage": {
            "required": list(ALL_BIASES),
            "assessed": [
                {"bias": b.bias, "assessment": b.assessment} for b in inside.bias_checks
            ],
            "missing": [b for b in ALL_BIASES if b not in covered],
        },
        "derivation": {
            "anchor": outside.aggregate_base_rate,
            "walk": walk,
            "implied": implied,
            "stated": forecast.probability,
            "drift": abs(forecast.probability - implied),
            "slack": th.derivation_slack,
        },
        "calibration_hygiene": {
            "probability": forecast.probability,
            "floor": th.calibration_floor,
            "ceiling": th.calibration_ceiling,
            "spread": worst_sub_claim_spread(outside),
            "agreement_threshold": th.reference_class_agreement,
            "justification": forecast.extreme_justification,
        },
    }


def run_forecast_checks_detailed(
    forecast: Forecast,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    t: CheckThresholds | None = None,
    sources_seen: Iterable[SourceRef] = (),
) -> list[CheckResult]:
    """Every forecast-side check, passes included, in `FORECAST_CHECK_LABELS` order.

    `run_forecast_checks` is a filter over this, so the two can never disagree about
    which checks exist.

    `sources_seen` defaults to empty so callers that only have the four graph artifacts
    still work — with nothing retrieved, `check_citations` has nothing to contradict and
    only fires on a URL the agent could not have seen.
    """
    th = _thresholds(t)
    violations = [
        check_decomposition(decomposition, th),
        check_linkage(forecast, decomposition, outside, inside, th),
        check_dragonfly(outside, th),
        check_aggregation(outside, decomposition, th),
        check_citations(outside, inside, sources_seen),
        check_signal_vs_noise(inside, th),
        check_disconfirming(inside, th),
        check_bias_coverage(inside, th),
        check_derivation(forecast, outside, inside, th),
        check_calibration_hygiene(forecast, outside, th),
    ]
    evidence = check_evidence(forecast, decomposition, outside, inside, th)
    return [
        CheckResult(
            principle=v.principle if v is not None else principle,
            name=name,
            label=label,
            passed=v is None,
            violation=v,
            evidence=evidence.get(name, {}),
        )
        for (name, principle, label), v in zip(FORECAST_CHECK_LABELS, violations)
    ]


def run_forecast_checks(
    forecast: Forecast,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    t: CheckThresholds | None = None,
    sources_seen: Iterable[SourceRef] = (),
) -> list[CheckViolation]:
    """Every forecast-side check. Called by the `Critique` node. Empty list is clean.

    Takes the pieces rather than a `ForecastState` so this module stays free of any
    dependency on `graphs`, which imports it. `sources_seen` arrives the same way — it
    lives on `ForecastDeps`, which this module also must not import.
    """
    return [
        r.violation
        for r in run_forecast_checks_detailed(
            forecast, decomposition, outside, inside, t, sources_seen
        )
        if r.violation is not None
    ]


def run_update_checks(
    d: UpdateDecision, t: CheckThresholds | None = None
) -> list[CheckViolation]:
    """Every update-side check. Called by the `GuardUpdate` node."""
    th = _thresholds(t)
    results = [check_bayes_direction(d, th), check_update_magnitude(d, th)]
    return [v for v in results if v is not None]


def blocking(violations: list[CheckViolation]) -> list[CheckViolation]:
    """The subset that should send a forecast back for another synthesis attempt."""
    return [v for v in violations if v.blocking]
