"""Tests for the methodology checks.

These are the highest-value unit tests in the codebase. Everything here is pure
logic that Pydantic cannot validate — a wrong sign in `check_bayes_direction` or a
flipped comparison in `check_derivation` produces plausible output and a wrong
answer, with nothing at runtime to flag it.

Each check gets a passing case and a failing case. Thresholds are monkeypatched
where the behaviour depends on them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config import get_check_thresholds
from superforecaster import checks
from superforecaster.models import (
    ALL_BIASES,
    Adjustment,
    BiasCheck,
    Decomposition,
    EvidenceItem,
    Forecast,
    InsideView,
    OutsideView,
    ReferenceClass,
    ResearchSummary,
    SubPrediction,
    UpdateDecision,
)


# ---------- factories ----------


def sub(
    question: str = "Will the deal be announced?",
    probability: float = 0.5,
    knowability: str = "researchable",
    rationale: str = "because",
) -> SubPrediction:
    return SubPrediction(
        question=question,
        probability=probability,
        rationale=rationale,
        confidence="medium",
        knowability=knowability,
    )


def decomposition(**kwargs) -> Decomposition:
    defaults = {
        "sub_claims": [sub(), sub(knowability="judgment"), sub()],
        "chain_note": "multiply the three",
    }
    return Decomposition(**{**defaults, **kwargs})


def ref(name: str = "all acquisitions", base_rate: float = 0.20) -> ReferenceClass:
    return ReferenceClass(
        name=name, base_rate=base_rate, sample_size=40, source="SEC filings"
    )


def outside(**kwargs) -> OutsideView:
    defaults = {
        "reference_classes": [ref("a", 0.20), ref("b", 0.24)],
        "aggregate_base_rate": 0.22,
        "disagreement": "",
    }
    return OutsideView(**{**defaults, **kwargs})


def adjustment(
    direction: str = "up",
    magnitude: float = 0.10,
    flip_test: str = "I would drop back to the base rate",
    is_noise: bool = False,
    evidence: str = "the board approved it",
) -> Adjustment:
    return Adjustment(
        evidence=evidence,
        direction=direction,
        magnitude=magnitude,
        flip_test=flip_test,
        is_noise=is_noise,
    )


def all_bias_checks() -> list[BiasCheck]:
    return [BiasCheck(bias=b, assessment=f"considered {b}") for b in ALL_BIASES]


def inside(**kwargs) -> InsideView:
    defaults = {
        "adjustments": [adjustment("up", 0.10), adjustment("down", 0.04)],
        "steel_man": "regulators could block it",
        "what_would_change_my_mind": "a second request from the FTC",
        "bias_checks": all_bias_checks(),
    }
    return InsideView(**{**defaults, **kwargs})


def forecast(probability: float = 0.28, confidence: str = "medium") -> Forecast:
    return Forecast(
        question="Will A acquire B?",
        resolution_criteria="Deal closes",
        resolution_date=datetime(2027, 1, 1, tzinfo=timezone.utc),
        category="business",
        probability=probability,
        confidence=confidence,
        decompositions=[sub(), sub(), sub()],
        research=ResearchSummary(),
        reasoning="base rate then adjustments",
    )


def update(
    prior: float = 0.50,
    posterior: float = 0.60,
    evidence: list[EvidenceItem] | None = None,
) -> UpdateDecision:
    if evidence is None:
        evidence = [
            EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.3),
        ]
    return UpdateDecision(
        evidence=evidence, prior=prior, posterior=posterior, reasoning="r"
    )


# ---------- P1 + P2: decomposition ----------


def test_decomposition_clean():
    assert checks.check_decomposition(decomposition()) is None


def test_decomposition_requires_chain_note():
    v = checks.check_decomposition(decomposition(chain_note="   "))
    assert v is not None
    assert v.principle == 1


def test_decomposition_requires_rationale():
    claims = [sub(), sub(rationale=""), sub()]
    v = checks.check_decomposition(decomposition(sub_claims=claims))
    assert v is not None
    assert v.principle == 1


def test_decomposition_rejects_all_judgment():
    """P2 — if nothing is researchable, no effort can be directed at base rates."""
    claims = [sub(knowability="judgment") for _ in range(3)]
    v = checks.check_decomposition(decomposition(sub_claims=claims))
    assert v is not None
    assert v.principle == 2
    assert v.name == "knowability"


def test_decomposition_all_judgment_catches_unlabeled_output():
    """`knowability` defaults to judgment, so an unlabeled decomposition fails too."""
    claims = [
        SubPrediction(question="q", probability=0.5, rationale="r", confidence="low")
        for _ in range(3)
    ]
    v = checks.check_decomposition(decomposition(sub_claims=claims))
    assert v is not None
    assert v.principle == 2


# ---------- P7: dragonfly eye ----------


def test_dragonfly_clean_when_classes_agree():
    """A small spread needs no explanation."""
    o = outside(reference_classes=[ref("a", 0.20), ref("b", 0.25)])
    assert checks.check_dragonfly(o) is None


def test_dragonfly_fails_on_silent_disagreement():
    """0.12 vs 0.55 averaged to 0.33 with no comment throws away real uncertainty."""
    o = outside(
        reference_classes=[ref("a", 0.12), ref("b", 0.55)],
        aggregate_base_rate=0.33,
        disagreement="",
    )
    v = checks.check_dragonfly(o)
    assert v is not None
    assert v.principle == 7


def test_dragonfly_passes_when_disagreement_is_explained():
    o = outside(
        reference_classes=[ref("a", 0.12), ref("b", 0.55)],
        aggregate_base_rate=0.33,
        disagreement="the narrow class excludes hostile bids, which is why it is lower",
    )
    assert checks.check_dragonfly(o) is None


def test_dragonfly_threshold_is_configurable(monkeypatch):
    o = outside(reference_classes=[ref("a", 0.20), ref("b", 0.45)])
    assert checks.check_dragonfly(o) is not None

    monkeypatch.setenv("CHECK_RC_DISAGREEMENT", "0.50")
    assert checks.check_dragonfly(o) is None


# ---------- P9: signal vs noise ----------


def test_signal_vs_noise_clean():
    assert checks.check_signal_vs_noise(inside()) is None


def test_signal_vs_noise_requires_flip_test():
    i = inside(adjustments=[adjustment(flip_test="  ")])
    v = checks.check_signal_vs_noise(i)
    assert v is not None
    assert v.principle == 9


def test_signal_vs_noise_rejects_moving_noise():
    """Evidence the agent itself called noise must move the number by zero."""
    i = inside(adjustments=[adjustment(is_noise=True, magnitude=0.08)])
    v = checks.check_signal_vs_noise(i)
    assert v is not None
    assert "noise" in v.detail


def test_signal_vs_noise_allows_zero_magnitude_noise():
    i = inside(adjustments=[adjustment(is_noise=True, magnitude=0.0), adjustment()])
    assert checks.check_signal_vs_noise(i) is None


# ---------- P14: disconfirming evidence ----------


def test_disconfirming_clean():
    assert checks.check_disconfirming(inside()) is None


def test_disconfirming_requires_steel_man():
    v = checks.check_disconfirming(inside(steel_man=""))
    assert v is not None
    assert v.principle == 14


def test_disconfirming_requires_what_would_change_my_mind():
    v = checks.check_disconfirming(inside(what_would_change_my_mind=""))
    assert v is not None


def test_disconfirming_rejects_one_sided_evidence():
    i = inside(
        adjustments=[
            adjustment("up", 0.10, evidence="a"),
            adjustment("up", 0.05, evidence="b"),
            adjustment("up", 0.03, evidence="c"),
        ]
    )
    v = checks.check_disconfirming(i)
    assert v is not None
    assert "no evidence was found against" in v.detail


def test_disconfirming_allows_a_single_adjustment():
    """With one piece of evidence there is nothing to be lopsided about."""
    i = inside(adjustments=[adjustment("up", 0.10)])
    assert checks.check_disconfirming(i) is None


def test_disconfirming_ignores_noise_when_counting_direction():
    i = inside(
        adjustments=[
            adjustment("up", 0.10, evidence="a"),
            adjustment("down", 0.0, is_noise=True, evidence="b"),
        ]
    )
    assert checks.check_disconfirming(i) is None


# ---------- P15: bias coverage ----------


def test_bias_coverage_clean():
    assert checks.check_bias_coverage(inside()) is None


def test_bias_coverage_rejects_duplicates():
    """The schema pins the list at five entries but cannot stop five of the same."""
    dupes = [BiasCheck(bias="anchoring", assessment="considered") for _ in range(5)]
    v = checks.check_bias_coverage(inside(bias_checks=dupes))
    assert v is not None
    assert v.principle == 15


def test_bias_coverage_rejects_empty_assessment():
    partial = all_bias_checks()
    partial[2] = BiasCheck(bias=partial[2].bias, assessment="   ")
    v = checks.check_bias_coverage(inside(bias_checks=partial))
    assert v is not None
    assert "not assessed" in v.detail


# ---------- P6: derivation / regression to the mean ----------


def test_derivation_clean():
    """base 0.22 + 0.10 up - 0.04 down = 0.28, and the forecast says 0.28."""
    assert checks.check_derivation(forecast(0.28), outside(), inside()) is None


def test_derivation_catches_narrative_drift():
    """Base rate 0.20, adjustments summing 0.10, final 0.75 — unsupported by its own evidence."""
    o = outside(
        reference_classes=[ref("a", 0.20), ref("b", 0.20)], aggregate_base_rate=0.20
    )
    i = inside(
        adjustments=[adjustment("up", 0.10), adjustment("down", 0.0, is_noise=True)]
    )
    v = checks.check_derivation(forecast(0.75), o, i)
    assert v is not None
    assert v.principle == 6


def test_derivation_allows_a_round_number():
    """P8 note: 0.60 is a legitimate answer when the arithmetic lands there."""
    o = outside(
        reference_classes=[ref("a", 0.50), ref("b", 0.50)], aggregate_base_rate=0.50
    )
    i = inside(adjustments=[adjustment("up", 0.10)])
    assert checks.check_derivation(forecast(0.60), o, i) is None


def test_derivation_ignores_noise_adjustments():
    o = outside(
        reference_classes=[ref("a", 0.30), ref("b", 0.30)], aggregate_base_rate=0.30
    )
    i = inside(
        adjustments=[
            adjustment("up", 0.10),
            adjustment("up", 0.0, is_noise=True, evidence="irrelevant"),
        ]
    )
    assert checks.check_derivation(forecast(0.40), o, i) is None


def test_derivation_clamps_to_unit_interval():
    """base 0.90 + 0.30 up would imply 1.20; it clamps to 1.00."""
    o = outside(
        reference_classes=[ref("a", 0.90), ref("b", 0.90)], aggregate_base_rate=0.90
    )
    i = inside(adjustments=[adjustment("up", 0.30)])
    assert checks.check_derivation(forecast(0.98), o, i) is None


def test_derivation_slack_is_configurable(monkeypatch):
    o = outside(
        reference_classes=[ref("a", 0.30), ref("b", 0.30)], aggregate_base_rate=0.30
    )
    i = inside(adjustments=[adjustment("up", 0.10)])
    assert checks.check_derivation(forecast(0.48), o, i) is not None

    monkeypatch.setenv("CHECK_DERIVATION_SLACK", "0.15")
    assert checks.check_derivation(forecast(0.48), o, i) is None


# ---------- P16: calibration hygiene ----------


def test_calibration_hygiene_clean_in_range():
    assert checks.check_calibration_hygiene(forecast(0.28), outside()) is None


def test_calibration_hygiene_rejects_unearned_extreme():
    v = checks.check_calibration_hygiene(forecast(0.995, "medium"), outside())
    assert v is not None
    assert v.principle == 16


def test_calibration_hygiene_allows_earned_extreme():
    """High confidence plus reference classes that agree earns the extreme."""
    o = outside(
        reference_classes=[ref("a", 0.97), ref("b", 0.99)], aggregate_base_rate=0.98
    )
    assert checks.check_calibration_hygiene(forecast(0.99, "high"), o) is None


def test_calibration_hygiene_rejects_extreme_when_classes_disagree():
    o = outside(
        reference_classes=[ref("a", 0.40), ref("b", 0.95)],
        aggregate_base_rate=0.70,
        disagreement="explained",
    )
    assert checks.check_calibration_hygiene(forecast(0.99, "high"), o) is not None


def test_calibration_bounds_are_configurable(monkeypatch):
    monkeypatch.setenv("CHECK_CALIBRATION_CEILING", "0.999")
    assert (
        checks.check_calibration_hygiene(forecast(0.995, "medium"), outside()) is None
    )


# ---------- P11: Bayesian direction ----------


def test_evidence_weight_is_positive_for_confirming_evidence():
    d = update(
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.1)]
    )
    assert checks.evidence_weight(d) > 0


def test_evidence_weight_is_zero_for_uninformative_evidence():
    """p_if_true == p_if_false is a fact that tells you nothing — log(1) = 0."""
    d = update(
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.5, p_if_false=0.5)]
    )
    assert checks.evidence_weight(d) == pytest.approx(0.0)


def test_evidence_weight_handles_zero_likelihood():
    """p_if_false == 0 would be an infinite ratio; it is clamped instead of raising."""
    d = update(
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.0)]
    )
    weight = checks.evidence_weight(d)
    assert weight > 0
    assert weight != float("inf")


def test_bayes_direction_clean_when_move_agrees():
    d = update(prior=0.50, posterior=0.65)
    assert checks.check_bayes_direction(d) is None


def test_bayes_direction_catches_confirming_evidence_with_falling_probability():
    """The headline case: says the evidence helps, then lowers the number."""
    d = update(
        prior=0.50,
        posterior=0.40,
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.1)],
    )
    v = checks.check_bayes_direction(d)
    assert v is not None
    assert v.principle == 11
    assert "net-confirming" in v.detail


def test_bayes_direction_catches_disconfirming_evidence_with_rising_probability():
    d = update(
        prior=0.50,
        posterior=0.70,
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.1, p_if_false=0.9)],
    )
    v = checks.check_bayes_direction(d)
    assert v is not None
    assert "net-disconfirming" in v.detail


def test_bayes_direction_catches_a_move_on_no_evidence():
    """An empty evidence list sums to zero weight, so any material move is unjustified."""
    d = update(prior=0.50, posterior=0.70, evidence=[])
    v = checks.check_bayes_direction(d)
    assert v is not None
    assert "no net weight" in v.detail


def test_bayes_direction_tolerates_a_trivial_move_on_neutral_evidence():
    d = update(prior=0.50, posterior=0.51, evidence=[])
    assert checks.check_bayes_direction(d) is None


def test_bayes_direction_sums_conflicting_evidence():
    """Two facts pointing opposite ways cancel; the net sign is what matters."""
    d = update(
        prior=0.50,
        posterior=0.62,
        evidence=[
            EvidenceItem(fact="up", source="s", p_if_true=0.9, p_if_false=0.2),
            EvidenceItem(fact="down", source="s", p_if_true=0.3, p_if_false=0.5),
        ],
    )
    assert checks.evidence_weight(d) > 0
    assert checks.check_bayes_direction(d) is None


# ---------- P10 + P12: update magnitude ----------


def test_update_magnitude_clean():
    assert checks.check_update_magnitude(update(0.50, 0.60)) is None


def test_update_magnitude_catches_under_reaction():
    """Real evidence arrived and the agent did not move at all."""
    d = update(prior=0.50, posterior=0.50)
    v = checks.check_update_magnitude(d)
    assert v is not None
    assert v.principle == 12
    assert "anchoring" in v.detail


def test_update_magnitude_does_not_fail_large_moves():
    """FTX filing for bankruptcy is a legitimate 0.20 -> 0.99 move."""
    d = update(
        prior=0.20,
        posterior=0.99,
        evidence=[
            EvidenceItem(
                fact="chapter 11 filed", source="s", p_if_true=0.99, p_if_false=0.01
            )
        ],
    )
    assert checks.check_update_magnitude(d) is None


def test_update_magnitude_ignores_no_move_on_no_evidence():
    """No evidence and no movement is correct behaviour, not under-reaction."""
    d = update(prior=0.50, posterior=0.50, evidence=[])
    assert checks.check_update_magnitude(d) is None


# ---------- P12: large-move routing ----------


def test_is_large_move_false_for_normal_update():
    assert checks.is_large_move(update(0.50, 0.60)) is False


def test_is_large_move_true_for_decisive_news():
    assert checks.is_large_move(update(0.10, 0.95)) is True


def test_large_move_threshold_is_configurable(monkeypatch):
    d = update(prior=0.20, posterior=0.70)
    assert checks.is_large_move(d) is False

    monkeypatch.setenv("CHECK_LARGE_MOVE", "0.40")
    assert checks.is_large_move(d) is True


# ---------- suites ----------


def test_run_forecast_checks_clean():
    violations = checks.run_forecast_checks(
        forecast(0.28), decomposition(), outside(), inside()
    )
    assert violations == []


def test_run_forecast_checks_collects_every_failure():
    """One input violating several principles reports all of them, not just the first."""
    o = outside(
        reference_classes=[ref("a", 0.10), ref("b", 0.60)],
        aggregate_base_rate=0.20,
        disagreement="",
    )
    i = inside(
        adjustments=[
            adjustment("up", 0.05, evidence="a"),
            adjustment("up", 0.05, evidence="b"),
        ],
        steel_man="",
    )
    violations = checks.run_forecast_checks(forecast(0.90), decomposition(), o, i)
    principles = {v.principle for v in violations}
    assert 7 in principles  # silent disagreement
    assert 14 in principles  # no steel man, one-sided
    assert 6 in principles  # 0.90 is nowhere near 0.20 + 0.10


def test_run_update_checks_clean():
    assert checks.run_update_checks(update(0.50, 0.62)) == []


def test_run_update_checks_reports_direction_and_magnitude():
    d = update(prior=0.50, posterior=0.50)
    violations = checks.run_update_checks(d)
    assert {v.principle for v in violations} == {12}


def test_blocking_filters_non_blocking_violations():
    v_block = checks.check_dragonfly(
        outside(reference_classes=[ref("a", 0.10), ref("b", 0.60)])
    )
    assert v_block is not None
    v_soft = v_block.model_copy(update={"blocking": False})
    assert checks.blocking([v_block, v_soft]) == [v_block]


def test_thresholds_default_when_none_passed():
    """Every check accepts t=None and reads config itself."""
    assert checks.check_dragonfly(outside(), None) is None
    assert get_check_thresholds().reference_class_disagreement == 0.20
