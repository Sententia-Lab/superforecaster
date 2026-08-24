"""Methodology checks — pure functions over structured output (ADR 13)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .config import CheckThresholds, get_check_thresholds
from .models import (
    ALL_BIASES,
    DEPENDENCE,
    Adjustment,
    ChainRule,
    CheckViolation,
    Decomposition,
    DependentGroup,
    Forecast,
    GradedSource,
    InsideView,
    OutsideView,
    ResearchedLens,
    SourceRef,
    UpdateDecision,
)

_LIKELIHOOD_FLOOR = 1e-6  # a likelihood of exactly 0 gives an infinite ratio
_EPSILON = 1e-9


def _thresholds(t: CheckThresholds | None) -> CheckThresholds:
    return t if t is not None else get_check_thresholds()


def clamp(p: float) -> float:
    return min(1.0, max(0.0, p))


def signed_adjustment(a: Adjustment) -> float:
    """An adjustment's contribution in probability points. Noise contributes nothing."""
    if a.is_noise or a.direction == "neutral":
        return 0.0
    return a.magnitude if a.direction == "up" else -a.magnitude


# ---------- Rates ----------


def lens_rate(lens: ResearchedLens) -> float:
    """A lens's base rate: pooled hits over pooled n across its evidence blocks."""
    n = sum(e.n for e in lens.evidence)
    return sum(e.hits for e in lens.evidence) / n if n > 0 else 0.0


def lens_sources(lens: ResearchedLens) -> list[GradedSource]:
    return [e.source for e in lens.evidence if e.source is not None]


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    """Weighted mean, or None when nothing carries weight."""
    total = sum(w for w, _ in pairs)
    if total <= _EPSILON:
        return None
    return sum(w * v for w, v in pairs) / total


def sub_question_rate(
    sub_question_id: str, o: OutsideView, i: InsideView | None = None
) -> float | None:
    """One sub-question's rate: its lenses, each moved by its own modifiers when `i` is
    given, blended by `weight` (fit, never sample size). None when no lens claims it."""
    pairs = []
    for lens in o.lenses:
        if sub_question_id not in lens.sub_question_ids:
            continue
        moved = sum(
            signed_adjustment(a)
            for a in (i.adjustments if i else [])
            if a.lens_name == lens.name
        )
        pairs.append((lens.weight, clamp(lens_rate(lens) + moved)))
    return _weighted_mean(pairs)


def weighted_base_rate(o: OutsideView) -> float | None:
    """The flat relevance-weighted mean over every lens. The `custom` chain's anchor."""
    return _weighted_mean([(l.weight, lens_rate(l)) for l in o.lenses])


def sub_question_spreads(o: OutsideView) -> dict[str | None, float]:
    """Max minus min lens rate within each sub-question. Across sub-questions the number
    means nothing: two lenses only disagree if they measure the same thing."""
    by_column: dict[str | None, list[float]] = {}
    for l in o.lenses:
        for key in l.sub_question_ids or [None]:
            by_column.setdefault(key, []).append(lens_rate(l))
    return {k: max(v) - min(v) for k, v in by_column.items()}


def worst_sub_question_spread(o: OutsideView) -> float:
    return max(sub_question_spreads(o).values(), default=0.0)


# ---------- The chain ----------


def _reduce(rates: list[float], rule: ChainRule, w: float) -> float:
    """The chain rule, `w` of the way from independent to the Fréchet-Hoeffding bound
    (the weakest member for a conjunction, the strongest for a disjunction)."""
    if rule == "conjunction":
        independent, bound = math.prod(rates), min(rates)
    else:
        independent, bound = 1.0 - math.prod(1.0 - p for p in rates), max(rates)
    return independent + w * (bound - independent)


def combine_sub_question_rates(
    rates: list[float],
    rule: ChainRule,
    groups: list[DependentGroup] | None = None,
) -> float | None:
    """What the chain the decomposition describes implies. Each dependent group reduces
    under its own parameter first; the group values and the ungrouped rates then reduce
    as independent (ADR 65). None for `custom`."""
    if not rates or rule not in ("conjunction", "disjunction"):
        return None
    groups = groups or []
    grouped = {m for g in groups for m in g.members}
    values = [
        _reduce([rates[m - 1] for m in g.members], rule, DEPENDENCE[g.kind])
        for g in groups
    ]
    values += [r for n, r in enumerate(rates, 1) if n not in grouped]
    return _reduce(values, rule, 0.0)


def chain_inputs(
    d: Decomposition, o: OutsideView, i: InsideView | None = None
) -> list[dict[str, Any]]:
    """Each sub-question's rate, in decomposition order. A sub-question nothing
    researched falls back to the decompose agent's own estimate, marked `estimated`,
    because a product over only the researched rates treats the rest as 1.0."""
    rows: list[dict[str, Any]] = []
    for s in d.sub_questions:
        researched = sub_question_rate(s.id, o, i) if s.id else None
        rows.append(
            {
                "id": s.id,
                "question": s.question,
                "rate": researched if researched is not None else s.probability,
                "source": "researched" if researched is not None else "estimated",
            }
        )
    return rows


def anchor_from(o: OutsideView, d: Decomposition | None) -> tuple[float | None, str]:
    """The pre-adjustment anchor the outside view implies, and which rule produced it."""
    if d is not None and d.chain_rule != "custom":
        rates = [row["rate"] for row in chain_inputs(d, o)]
        combined = combine_sub_question_rates(rates, d.chain_rule, d.dependent_groups)
        if combined is not None:
            return combined, d.chain_rule
    return weighted_base_rate(o), "weighted mean"


def implied_probability(
    o: OutsideView, i: InsideView, d: Decomposition | None = None
) -> float:
    """P6. Each lens moved by its own modifiers, blended per sub-question, combined by
    the chain rule. Adjustments naming no lens apply to the combined value."""
    whole_question = sum(signed_adjustment(a) for a in i.adjustments if not a.lens_name)
    if d is None or d.chain_rule == "custom":
        return clamp(
            o.aggregate_base_rate + sum(signed_adjustment(a) for a in i.adjustments)
        )
    rates = [row["rate"] for row in chain_inputs(d, o, i)]
    combined = combine_sub_question_rates(rates, d.chain_rule, d.dependent_groups)
    if combined is None:
        return clamp(o.aggregate_base_rate + whole_question)
    return clamp(combined + whole_question)


# ---------- Decomposition ----------


def check_decomposition(
    d: Decomposition, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P1 + P2. Every sub-question has a rationale, and not everything is judgment."""
    if not d.chain_note.strip():
        return CheckViolation(
            principle=1,
            name="decomposition",
            detail="chain_note is empty — the sub-questions were listed but never "
            "combined into the whole question",
        )
    unexplained = [s.question for s in d.sub_questions if not s.rationale.strip()]
    if unexplained:
        return CheckViolation(
            principle=1,
            name="decomposition",
            detail=f"sub-questions with no rationale: {unexplained}",
        )
    if all(s.knowability == "judgment" for s in d.sub_questions):
        return CheckViolation(
            principle=2,
            name="knowability",
            detail="every sub-question is labeled 'judgment' — nothing was identified as "
            "researchable, so no effort can be directed at lookup-able base rates",
        )
    return None


def check_linkage(
    d: Decomposition,
    o: OutsideView,
    i: InsideView,
    t: CheckThresholds | None = None,
) -> CheckViolation | None:
    """P1. A reference back to a sub-question has to point at one that exists."""
    known = {s.id for s in d.sub_questions if s.id}
    referenced = {
        cid for holder in (*o.lenses, *i.adjustments) for cid in holder.sub_question_ids
    }
    unknown = sorted(referenced - known)
    if unknown:
        return CheckViolation(
            principle=1,
            name="linkage",
            detail=f"sub-question id{'s' if len(unknown) != 1 else ''} "
            f"{', '.join(unknown)} referenced but not in the decomposition "
            f"({', '.join(sorted(known)) or 'none'})",
        )
    return None


# ---------- Outside view ----------


def check_base_rate_derivation(
    o: OutsideView, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P4. A counted block's `n` and `hits` match its named analogs; a published block
    names its source."""
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
            principle=4, name="base_rates", detail="; ".join(problems)
        )
    return None


def check_dragonfly(
    o: OutsideView, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P7. A material disagreement between lenses on one sub-question gets explained."""
    th = _thresholds(t)
    spreads = sub_question_spreads(o)
    if not spreads or o.disagreement.strip():
        return None
    worst_id, spread = max(spreads.items(), key=lambda kv: kv[1])
    if spread <= th.reference_class_disagreement:
        return None
    rates = ", ".join(
        f"{l.name}={lens_rate(l):.2f}"
        for l in o.lenses
        if worst_id in (l.sub_question_ids or [None])
    )
    where = f"for {worst_id}" if worst_id else "for the question as a whole"
    return CheckViolation(
        principle=7,
        name="dragonfly",
        detail=f"reference classes {where} span {spread:.2f} "
        f"(> {th.reference_class_disagreement:.2f}) but `disagreement` is empty: {rates}",
    )


def check_aggregation(
    o: OutsideView,
    d: Decomposition | None = None,
    t: CheckThresholds | None = None,
) -> CheckViolation | None:
    """P7. The anchor is what the lenses and the chain rule imply. The merge computes it
    with `anchor_from` too, so this guards the artifact — a hand-built fixture, an old
    payload — rather than a model's arithmetic (ADR 33)."""
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
    if drift <= th.aggregate_slack:
        return None
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


def check_citations(
    o: OutsideView, i: InsideView, seen: Iterable[SourceRef]
) -> CheckViolation | None:
    """P4. Every cited URL is one a tool actually returned."""
    retrieved = {ref.url for ref in seen if ref.url}
    cited = [s for l in o.lenses for s in lens_sources(l)]
    cited += [s for a in i.adjustments for s in a.sources]
    invented = sorted({s.url for s in cited if s.url and s.url not in retrieved})
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
    """P9. Every adjustment has a flip test, and evidence marked noise moves nothing."""
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
    """P14. The opposing case is stated, and the evidence is not all one-sided."""
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
        return CheckViolation(
            principle=14,
            name="disconfirming",
            detail=f"all {len(real)} adjustments point '{real[0].direction}' — no "
            "evidence was found against the emerging conclusion",
        )
    return None


def check_bias_coverage(
    i: InsideView, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P15. All five named biases are addressed, each with something said."""
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
    """P6. The final probability is within `derivation_slack` of what the lenses,
    modifiers, and chain rule imply."""
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
    """P16, advisory (ADR 29). An extreme is justified, not forbidden: flag a
    probability outside the band with no justification, or one hugging the band edge
    while the lenses on some sub-question disagree."""
    th = _thresholds(t)
    p = f.probability
    justified = bool(f.extreme_justification.strip())
    spread = worst_sub_question_spread(o)

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
    """P11. Total weight of evidence as a sum of log likelihood ratios,
    log(p_if_true / p_if_false) per fact. Its sign is what `check_bayes_direction`
    reads; a fact with equal likelihoods contributes zero."""
    total = 0.0
    for e in d.evidence:
        true_likelihood = max(e.p_if_true, _LIKELIHOOD_FLOOR)
        false_likelihood = max(e.p_if_false, _LIKELIHOOD_FLOOR)
        total += math.log(true_likelihood / false_likelihood)
    return total


def check_bayes_direction(
    d: UpdateDecision, t: CheckThresholds | None = None
) -> CheckViolation | None:
    """P11. The probability moved the way the agent's own likelihoods point. Sign only;
    the methodology does not require the exact posterior."""
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
    """P10 + P12. Under-reaction only: evidence carries weight but the number did not
    move. A large move is verified, not capped (ADR 16)."""
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
    """P12. Whether this update is big enough to demand a verification pass."""
    return abs(d.posterior - d.prior) > _thresholds(t).large_move


# ---------- Suites ----------


@dataclass(frozen=True)
class _Ctx:
    f: Forecast
    d: Decomposition
    o: OutsideView
    i: InsideView
    seen: Iterable[SourceRef]
    t: CheckThresholds | None


FORECAST_CHECKS: tuple[tuple[str, Any], ...] = (
    ("decomposition", lambda c: check_decomposition(c.d, c.t)),
    ("linkage", lambda c: check_linkage(c.d, c.o, c.i, c.t)),
    ("base_rates", lambda c: check_base_rate_derivation(c.o)),
    ("dragonfly", lambda c: check_dragonfly(c.o, c.t)),
    ("aggregation", lambda c: check_aggregation(c.o, c.d, c.t)),
    ("citations", lambda c: check_citations(c.o, c.i, c.seen)),
    ("signal_vs_noise", lambda c: check_signal_vs_noise(c.i, c.t)),
    ("disconfirming", lambda c: check_disconfirming(c.i, c.t)),
    ("bias_coverage", lambda c: check_bias_coverage(c.i, c.t)),
    ("derivation", lambda c: check_derivation(c.f, c.o, c.i, c.d, c.t)),
    ("calibration_hygiene", lambda c: check_calibration_hygiene(c.f, c.o, c.t)),
)
"""(name, how to run it), in the order the checks run."""


def run_forecast_checks(
    forecast: Forecast,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    t: CheckThresholds | None = None,
    sources_seen: Iterable[SourceRef] = (),
) -> list[CheckViolation]:
    """Every forecast-side check. Empty list is clean."""
    ctx = _Ctx(forecast, decomposition, outside, inside, list(sources_seen), t)
    return [v for _name, run in FORECAST_CHECKS if (v := run(ctx)) is not None]


def run_update_checks(
    d: UpdateDecision, t: CheckThresholds | None = None
) -> list[CheckViolation]:
    th = _thresholds(t)
    results = [check_bayes_direction(d, th), check_update_magnitude(d, th)]
    return [v for v in results if v is not None]


def blocking(violations: list[CheckViolation]) -> list[CheckViolation]:
    return [v for v in violations if v.blocking]
