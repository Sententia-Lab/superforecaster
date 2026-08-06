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
    ResearchedLens,
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


def _clamp(p: float) -> float:
    """A probability, whatever the arithmetic wanted to say."""
    return min(1.0, max(0.0, p))


def implied_probability(
    o: OutsideView, i: InsideView, d: Decomposition | None = None
) -> float:
    """P6. Where the adjusted lenses land once the decomposition's chain is applied.

    Each lens moves by its own modifiers; each sub-question blends its adjusted lenses by
    relevance; the sub-questions combine by `chain_rule`. The same
    `combine_sub_claim_rates` builds the anchor, so the anchor and the forecast are
    computed the same way and `check_derivation` is checking something real — it used to
    compare a flat sum against a number that same flat sum had suggested.

    Adjustments naming no lens — the reflect pass, the whole-question fallback — apply to
    the combined value rather than to any one population.

    `custom` has no chain to apply and keeps the flat sum on the anchor, which is what
    this did for everything before lenses existed.

    Clamped to [0, 1]: a chain of adjustments can walk off either end, and a forecast is
    a probability regardless of what the arithmetic wanted to say.
    """
    whole_question = sum(
        signed_adjustment(a) for a in i.adjustments if not a.lens_name
    )

    if d is None or d.chain_rule == "custom":
        return _clamp(o.aggregate_base_rate + sum(
            signed_adjustment(a) for a in i.adjustments
        ))

    rates = [row["rate"] for row in chain_inputs(d, o, i)]
    combined = combine_sub_claim_rates(rates, d.chain_rule)
    if combined is None:
        return _clamp(o.aggregate_base_rate + whole_question)
    return _clamp(combined + whole_question)


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
    rates = [lens_rate(l) for l in o.lenses]
    return max(rates) - min(rates) if rates else 0.0


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
    for l in o.lenses:
        for key in l.sub_claim_ids or [None]:
            by_column.setdefault(key, []).append(lens_rate(l))
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


def chain_inputs(
    d: Decomposition, o: OutsideView, i: InsideView | None = None
) -> list[dict[str, Any]]:
    """Each sub-claim's rate and where it came from, in decomposition order.

    Runs over **every** sub-claim, not just the researched ones. A conjunction over only
    the researched rates silently treats the rest as 1.0:

        prod([0.55, 0.70, 0.60])        = 0.231   sc4 vanishes
        prod([0.55, 0.70, 0.60, 0.80])  = 0.185   sc4 contributes its own estimate

    So a column nothing researched falls back to `SubPrediction.probability` — the
    decompose agent's own working estimate, an existing typed field, marked `estimated`
    so a reader can tell the two apart.

    With an inside view the rates are post-adjustment, which is what `implied_probability`
    walks. Without one they are the anchor. Same rows either way, so the two views cannot
    disagree about which sub-questions exist or in what order.
    """
    rows: list[dict[str, Any]] = []
    for s in d.sub_claims:
        researched = sub_claim_rate(s.id, o, i) if s.id else None
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


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    """Weighted mean, or None when nothing carries weight.

    One primitive for what was three near-identical guard-and-divide bodies: source
    support, the flat lens mean, and a sub-question's blend. The `None` is the shared
    part — a division by zero weight is not zero, it is "no answer".
    """
    total = sum(w for w, _ in pairs)
    if total <= _EPSILON:
        return None
    return sum(w * v for w, v in pairs) / total


def _weighted_support(pairs: list[tuple[float, SourceConfidence]]) -> float | None:
    """Weighted mean rank over (weight, grade)."""
    return _weighted_mean([(w, _CONFIDENCE_RANK[c]) for w, c in pairs])


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
        [(l.weight, claim_support(lens_sources(l))) for l in o.lenses]
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


def check_base_rate_derivation(
    o: OutsideView, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P4. Every base rate is arithmetic over cases, not a number someone liked.

    This is the check that makes "7 of the 10 cases I found did, so 70%" mean something.
    Two arms, one per kind of evidence:

    - **counted** — the block claims to have enumerated cases, so the analogs must back
      it: `n` equals how many were listed for this lens and `hits` equals how many of
      them resolved YES. A count with no cases behind it is an assertion wearing a
      fraction's clothes.
    - **published** — nobody can list 230 cases, so the audit is provenance instead: the
      statistic needs a source. `check_citations` separately verifies the URL was one the
      tools actually retrieved.

    Only lenses carrying a `counted` block are audited against analogs. A lens built
    entirely from published statistics legitimately has none.
    """
    problems: list[str] = []
    for lens in o.lenses:
        counted = [e for e in lens.evidence if e.kind == "counted"]
        if counted:
            claimed_n = sum(e.n for e in counted)
            claimed_hits = sum(e.hits for e in counted)
            listed = len(lens.analogs)
            resolved_yes = sum(1 for a in lens.analogs if a.outcome >= 1.0)
            if listed != claimed_n:
                problems.append(
                    f"{lens.name}: counted {claimed_n} cases but listed {listed} analogs"
                )
            elif resolved_yes != claimed_hits:
                problems.append(
                    f"{lens.name}: counted {claimed_hits} hits but {resolved_yes} of its "
                    f"{listed} analogs resolved yes"
                )
        for e in lens.evidence:
            if e.kind == "published" and e.source is None:
                problems.append(f"{lens.name}: a published statistic with no source")

    if problems:
        return CheckViolation(
            principle=4,
            name="base_rates",
            detail="; ".join(problems),
        )
    return None


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
        f"{l.name}={lens_rate(l):.2f}"
        for l in o.lenses
        if worst_id in (l.sub_claim_ids or [None])
    )
    where = f"for {worst_id}" if worst_id else "for the question as a whole"
    return CheckViolation(
        principle=7,
        name="dragonfly",
        detail=f"reference classes {where} span {spread:.2f} "
        f"(> {th.reference_class_disagreement:.2f}) but `disagreement` is empty: {rates}",
    )


def lens_rate(lens: ResearchedLens) -> float:
    """A lens's base rate: pooled hits over pooled n across its evidence.

    Derived, never asserted. Counted cases and published statistics share one
    denominator — `7/10` enumerated plus `140/230` published is `147/240`, one rate you
    can audit block by block. That is the difference between "70%" and "7 of the 10 cases
    I found did, and here they are".
    """
    n = sum(e.n for e in lens.evidence)
    if n <= 0:
        return 0.0
    return sum(e.hits for e in lens.evidence) / n


def adjusted_lens_rate(lens: ResearchedLens, i: InsideView | None = None) -> float:
    """A lens's rate after its own modifiers, clamped.

    Only the adjustments naming this lens apply. A modifier is meaningful relative to a
    population — "market cap exploded" is already inside *large-cap tech IPOs* and
    warrants nothing, while against *all AI labs* it is the whole differentiator. Moving
    a blended rate would double-count against the populations that already control for it.
    """
    moved = 0.0
    if i is not None:
        moved = sum(
            signed_adjustment(a) for a in i.adjustments if a.lens_name == lens.name
        )
    return min(1.0, max(0.0, lens_rate(lens) + moved))


def lenses_for(sub_claim_id: str, o: OutsideView) -> list[ResearchedLens]:
    """The lenses that say they inform this sub-claim."""
    return [l for l in o.lenses if sub_claim_id in l.sub_claim_ids]


def sub_claim_rate(
    sub_claim_id: str, o: OutsideView, i: InsideView | None = None
) -> float | None:
    """What one sub-question is worth: its adjusted lenses, blended by relevance.

        Σ(weightᵢ × adjusted_rateᵢ) / Σ weightᵢ

    **`n` is deliberately absent.** A lens measured over 12 cases can and should outweigh
    one measured over 230 when it fits better: sample size says how well a population was
    *measured*, not how much it *resembles this case*, and only the second is what a
    reference class is for. Discounting a well-chosen lens because its data was harder to
    gather would penalise exactly the questions where forecasting is hardest.

    `weight` is the one number in this pipeline nothing can verify, which is why
    `Lens.weight_rationale` is required and the UI shows each lens's own rate.

    Pass `i` to blend the *adjusted* rates — the inside view's answer for this
    sub-question. Without it you get the pre-adjustment anchor.

    None when no lens claims this sub-claim: the honest answer for a `judgment`
    sub-claim, and for a `researchable` one it means the research did not land.
    """
    return _weighted_mean(
        [(l.weight, adjusted_lens_rate(l, i)) for l in lenses_for(sub_claim_id, o)]
    )


def weighted_base_rate(o: OutsideView) -> float | None:
    """The flat relevance-weighted mean across every lens, ignoring the decomposition.

    Only the `custom` arm uses this — when the sub-questions have no formula relating
    them there is no chain to walk, and the honest fallback is what all the populations
    say together.
    """
    return _weighted_mean([(l.weight, lens_rate(l)) for l in o.lenses])


def anchor_from(o: OutsideView, d: Decomposition | None) -> tuple[float | None, str]:
    """The anchor the outside view implies, and which rule produced it.

    Pre-adjustment by construction: `chain_inputs` is called without an inside view, so
    this is where the lenses sat before any modifier moved them. One function so the
    outside-view merge and `check_aggregation` cannot disagree about which arm they used.
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
                f"{l.name}={lens_rate(l):.2f}@{l.weight:.2f}" for l in o.lenses
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
        for holder in (*o.lenses, *i.adjustments)
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


def lens_sources(lens: ResearchedLens) -> list[GradedSource]:
    """The sources behind a lens, which now live on its evidence blocks.

    A `published` block must carry one — that is how a statistic nobody could enumerate
    stays auditable. A `counted` block usually does not: its audit is the analogs.
    """
    return [e.source for e in lens.evidence if e.source is not None]


def _cited_sources(o: OutsideView, i: InsideView) -> list[GradedSource]:
    return [s for l in o.lenses for s in lens_sources(l)] + [
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
    d: Decomposition | None = None,
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
    implied = implied_probability(o, i, d)

    drift = abs(f.probability - implied)
    if drift > th.derivation_slack:
        return CheckViolation(
            principle=6,
            name="derivation",
            detail=f"final probability {f.probability:.3f} is {drift:.3f} away from the "
            f"{implied:.3f} its own lenses imply once each is moved by its own modifiers "
            f"and the sub-questions are combined (slack {th.derivation_slack:.3f})",
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


FORECAST_CHECKS: tuple[tuple[str, int, str, Any], ...] = (
    ("decomposition", 1, "P1 · P2 decomposition", lambda c: check_decomposition(c.d, c.t)),
    ("linkage", 1, "P1 sub-claim linkage", lambda c: check_linkage(c.f, c.d, c.o, c.i, c.t)),
    ("base_rates", 4, "P4 base rates derived", lambda c: check_base_rate_derivation(c.o)),
    ("dragonfly", 7, "P7 dragonfly", lambda c: check_dragonfly(c.o, c.t)),
    ("aggregation", 7, "P7 base-rate aggregation", lambda c: check_aggregation(c.o, c.d, c.t)),
    ("citations", 4, "P4 citations", lambda c: check_citations(c.o, c.i, c.seen)),
    ("signal_vs_noise", 9, "P9 signal vs noise", lambda c: check_signal_vs_noise(c.i, c.t)),
    ("disconfirming", 14, "P14 disconfirming", lambda c: check_disconfirming(c.i, c.t)),
    ("bias_coverage", 15, "P15 bias coverage", lambda c: check_bias_coverage(c.i, c.t)),
    ("derivation", 6, "P6 derivation", lambda c: check_derivation(c.f, c.o, c.i, c.d, c.t)),
    ("calibration_hygiene", 16, "P16 calibration hygiene", lambda c: check_calibration_hygiene(c.f, c.o, c.t)),
)
"""(slot name, principle, label, how to run it) in the order the checks run.

One table, not three. This used to be a labels tuple, a positionally-zipped list of
calls, and a dict of evidence projections keyed by the same names — three places that had
to stay in lockstep by hand, where reordering one silently mislabelled every row and a
typo'd key silently yielded `{}`.

The lambdas exist so the checks keep their narrow signatures. `evals/components.py` calls
`check_dragonfly(out)` with an outside view and nothing else; forcing every check to take
a context object would break that for no gain.

A check that fails may report a different `name` than the slot it occupies —
`check_decomposition` returns "knowability" for its P2 arm — so the slot name identifies
the row and the violation carries its own, more specific one.
"""


@dataclass(frozen=True)
class _Ctx:
    """Everything any check might want. Assembled once per run."""

    f: Forecast
    d: Decomposition
    o: OutsideView
    i: InsideView
    seen: Iterable[SourceRef]
    t: CheckThresholds | None


def run_forecast_checks(
    forecast: Forecast,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    t: CheckThresholds | None = None,
    sources_seen: Iterable[SourceRef] = (),
) -> list[CheckViolation]:
    """Every forecast-side check. Called by the `critique` step. Empty list is clean.

    Takes the pieces rather than a `ForecastState` so this module stays free of any
    dependency on `graphs`, which imports it. `sources_seen` arrives the same way — it
    lives on `ForecastDeps`, which this module also must not import.
    """
    ctx = _Ctx(forecast, decomposition, outside, inside, list(sources_seen), t)
    return [v for _n, _p, _l, run in FORECAST_CHECKS if (v := run(ctx)) is not None]


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
