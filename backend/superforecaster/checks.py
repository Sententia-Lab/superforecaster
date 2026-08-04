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

from config import CheckThresholds, get_check_thresholds

from .models import (
    ALL_BIASES,
    Adjustment,
    CheckViolation,
    Decomposition,
    Forecast,
    InsideView,
    OutsideView,
    UpdateDecision,
)

# Likelihoods of exactly 0 give an infinite likelihood ratio. Clamp instead.
_LIKELIHOOD_FLOOR = 1e-6

# Below this, a float comparison is treated as "no difference".
_EPSILON = 1e-9


def _thresholds(t: CheckThresholds | None) -> CheckThresholds:
    return t if t is not None else get_check_thresholds()


def _signed(a: Adjustment) -> float:
    """An adjustment's contribution in probability points. Noise contributes nothing."""
    if a.is_noise or a.direction == "neutral":
        return 0.0
    return a.magnitude if a.direction == "up" else -a.magnitude


def _spread(o: OutsideView) -> float:
    """How far apart the reference classes are."""
    rates = [rc.base_rate for rc in o.reference_classes]
    return max(rates) - min(rates) if rates else 0.0


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
    """
    th = _thresholds(t)
    spread = _spread(o)
    if spread > th.reference_class_disagreement and not o.disagreement.strip():
        rates = ", ".join(
            f"{rc.name}={rc.base_rate:.2f}" for rc in o.reference_classes
        )
        return CheckViolation(
            principle=7,
            name="dragonfly",
            detail=f"reference classes span {spread:.2f} "
            f"(> {th.reference_class_disagreement:.2f}) but `disagreement` is empty: {rates}",
        )
    return None


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
    implied = o.aggregate_base_rate + sum(_signed(a) for a in i.adjustments)
    implied = min(1.0, max(0.0, implied))

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
    """P16. Near-certainty has to be earned, not asserted.

    A well-calibrated 60% beats a miscalibrated 90%. Probabilities outside the
    configured floor/ceiling are allowed, but only when the agent is confident *and*
    its reference classes broadly agree — that is, when the outside view itself
    supports an extreme rather than the narrative doing all the work.
    """
    th = _thresholds(t)
    p = f.probability
    if th.calibration_floor <= p <= th.calibration_ceiling:
        return None

    spread = _spread(o)
    earned = f.confidence == "high" and spread <= th.reference_class_agreement
    if earned:
        return None

    return CheckViolation(
        principle=16,
        name="calibration_hygiene",
        detail=f"probability {p:.3f} is outside "
        f"[{th.calibration_floor:.2f}, {th.calibration_ceiling:.2f}] with "
        f"confidence='{f.confidence}' and a reference-class spread of {spread:.2f} "
        f"(needs 'high' and <= {th.reference_class_agreement:.2f})",
    )


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


def run_forecast_checks(
    forecast: Forecast,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    t: CheckThresholds | None = None,
) -> list[CheckViolation]:
    """Every forecast-side check. Called by the `Critique` node. Empty list is clean.

    Takes the pieces rather than a `ForecastState` so this module stays free of any
    dependency on `graphs`, which imports it.
    """
    th = _thresholds(t)
    results = [
        check_decomposition(decomposition, th),
        check_dragonfly(outside, th),
        check_signal_vs_noise(inside, th),
        check_disconfirming(inside, th),
        check_bias_coverage(inside, th),
        check_derivation(forecast, outside, inside, th),
        check_calibration_hygiene(forecast, outside, th),
    ]
    return [v for v in results if v is not None]


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
