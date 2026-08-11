"""Tests for the methodology checks.

These are the highest-value unit tests in the codebase. Everything here is pure
logic that Pydantic cannot validate — a wrong sign in `check_bayes_direction` or a
flipped comparison in `check_derivation` produces plausible output and a wrong
answer, with nothing at runtime to flag it.

Each check gets a passing case and a failing case. Thresholds are monkeypatched
where the behaviour depends on them.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from config import get_check_thresholds
from superforecaster import checks
from superforecaster.models import (
    ALL_BIASES,
    Adjustment,
    BiasCheck,
    Decomposition,
    DependentGroup,
    Evidence,
    EvidenceItem,
    Forecast,
    HistoricalAnalog,
    GradedSource,
    InsideView,
    OutsideView,
    ResearchedLens,
    ResearchSummary,
    SourceRef,
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
        knowability=knowability,
    )


def decomposition(**kwargs) -> Decomposition:
    defaults = {
        "sub_questions": [sub(), sub(knowability="judgment"), sub()],
        "chain_note": "multiply the three",
    }
    return Decomposition(**{**defaults, **kwargs})


def graded(
    source: str = "SEC filings",
    confidence: str = "high",
    url: str | None = None,
) -> GradedSource:
    return GradedSource(
        source=source, confidence=confidence, url=url, note="directly on point"
    )


def ref(
    name: str = "all acquisitions",
    base_rate: float = 0.20,
    weight: float = 1.0,
    sources: list[GradedSource] | None = None,
    n: int = 1000,
    sub_question_ids: list[str] | None = None,
) -> ResearchedLens:
    """One measured population.

    Takes a `base_rate` for readability even though the model no longer has that field —
    it is converted into a published evidence block of the right shape, so a test that
    says "a 20% lens" reads as one while still exercising the derivation.

    `n` defaults large so any two-decimal rate is exact: at n=40, 0.24 rounds to 10/40 =
    0.25 and every downstream assertion drifts.
    """
    hits = round(base_rate * n)
    return ResearchedLens(
        name=name,
        population=f"cases comparable to {name}",
        why_it_fits="it is the population this sub-question belongs to",
        weight=weight,
        weight_rationale="the closest available population",
        evidence=[
            Evidence(
                kind="published",
                hits=hits,
                n=n,
                note=f"{hits} of {n}",
                source=(sources or [graded()])[0],
            )
        ],
        sub_question_ids=sub_question_ids or [],
    )


def counted_ref(
    name: str = "counted cases",
    hits: int = 7,
    n: int = 10,
    weight: float = 1.0,
    sub_question_ids: list[str] | None = None,
) -> ResearchedLens:
    """A population measured by enumerating cases, with the analogs to prove it."""
    return ResearchedLens(
        name=name,
        population=f"cases comparable to {name}",
        why_it_fits="enumerable",
        weight=weight,
        weight_rationale="hand-counted",
        evidence=[Evidence(kind="counted", hits=hits, n=n, note="enumerated")],
        analogs=[
            HistoricalAnalog(
                description=f"case {i}",
                outcome=1.0 if i < hits else 0.0,
                relevance="same population",
            )
            for i in range(n)
        ],
        sub_question_ids=sub_question_ids or [],
    )


def outside(**kwargs) -> OutsideView:
    defaults = {
        "lenses": [ref("a", 0.20), ref("b", 0.24)],
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
    sources: list[GradedSource] | None = None,
) -> Adjustment:
    return Adjustment(
        evidence=evidence,
        direction=direction,
        magnitude=magnitude,
        flip_test=flip_test,
        is_noise=is_noise,
        sources=sources if sources is not None else [graded()],
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


def forecast(probability: float = 0.28, extreme_justification: str = "") -> Forecast:
    return Forecast(
        question="Will A acquire B?",
        resolution_criteria="Deal closes",
        resolution_date=datetime(2027, 1, 1, tzinfo=timezone.utc),
        category="business",
        probability=probability,
        decompositions=[sub(), sub(), sub()],
        research=ResearchSummary(),
        reasoning="base rate then adjustments",
        extreme_justification=extreme_justification,
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
    v = checks.check_decomposition(decomposition(sub_questions=claims))
    assert v is not None
    assert v.principle == 1


def test_decomposition_rejects_all_judgment():
    """P2 — if nothing is researchable, no effort can be directed at base rates."""
    claims = [sub(knowability="judgment") for _ in range(3)]
    v = checks.check_decomposition(decomposition(sub_questions=claims))
    assert v is not None
    assert v.principle == 2
    assert v.name == "knowability"


def test_decomposition_all_judgment_catches_unlabeled_output():
    """`knowability` defaults to judgment, so an unlabeled decomposition fails too."""
    claims = [
        SubPrediction(question="q", probability=0.5, rationale="r", confidence="low")
        for _ in range(3)
    ]
    v = checks.check_decomposition(decomposition(sub_questions=claims))
    assert v is not None
    assert v.principle == 2


# ---------- P7: dragonfly eye ----------


def test_dragonfly_clean_when_classes_agree():
    """A small spread needs no explanation."""
    o = outside(lenses=[ref("a", 0.20), ref("b", 0.25)])
    assert checks.check_dragonfly(o) is None


def test_dragonfly_fails_on_silent_disagreement():
    """0.12 vs 0.55 averaged to 0.33 with no comment throws away real uncertainty."""
    o = outside(
        lenses=[ref("a", 0.12), ref("b", 0.55)],
        aggregate_base_rate=0.33,
        disagreement="",
    )
    v = checks.check_dragonfly(o)
    assert v is not None
    assert v.principle == 7


def test_dragonfly_passes_when_disagreement_is_explained():
    o = outside(
        lenses=[ref("a", 0.12), ref("b", 0.55)],
        aggregate_base_rate=0.33,
        disagreement="the narrow class excludes hostile bids, which is why it is lower",
    )
    assert checks.check_dragonfly(o) is None


def test_dragonfly_threshold_is_configurable(monkeypatch):
    o = outside(lenses=[ref("a", 0.20), ref("b", 0.45)])
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
    o = outside(lenses=[ref("a", 0.20), ref("b", 0.20)], aggregate_base_rate=0.20)
    i = inside(
        adjustments=[adjustment("up", 0.10), adjustment("down", 0.0, is_noise=True)]
    )
    v = checks.check_derivation(forecast(0.75), o, i)
    assert v is not None
    assert v.principle == 6


def test_derivation_allows_a_round_number():
    """P8 note: 0.60 is a legitimate answer when the arithmetic lands there."""
    o = outside(lenses=[ref("a", 0.50), ref("b", 0.50)], aggregate_base_rate=0.50)
    i = inside(adjustments=[adjustment("up", 0.10)])
    assert checks.check_derivation(forecast(0.60), o, i) is None


def test_derivation_ignores_noise_adjustments():
    o = outside(lenses=[ref("a", 0.30), ref("b", 0.30)], aggregate_base_rate=0.30)
    i = inside(
        adjustments=[
            adjustment("up", 0.10),
            adjustment("up", 0.0, is_noise=True, evidence="irrelevant"),
        ]
    )
    assert checks.check_derivation(forecast(0.40), o, i) is None


def test_derivation_clamps_to_unit_interval():
    """base 0.90 + 0.30 up would imply 1.20; it clamps to 1.00."""
    o = outside(lenses=[ref("a", 0.90), ref("b", 0.90)], aggregate_base_rate=0.90)
    i = inside(adjustments=[adjustment("up", 0.30)])
    assert checks.check_derivation(forecast(0.98), o, i) is None


def test_derivation_slack_is_configurable(monkeypatch):
    o = outside(lenses=[ref("a", 0.30), ref("b", 0.30)], aggregate_base_rate=0.30)
    i = inside(adjustments=[adjustment("up", 0.10)])
    assert checks.check_derivation(forecast(0.48), o, i) is not None

    monkeypatch.setenv("CHECK_DERIVATION_SLACK", "0.15")
    assert checks.check_derivation(forecast(0.48), o, i) is None


# ---------- P16: calibration hygiene ----------


def test_calibration_hygiene_clean_in_range():
    assert checks.check_calibration_hygiene(forecast(0.28), outside()) is None


def test_calibration_hygiene_flags_unargued_extreme():
    v = checks.check_calibration_hygiene(forecast(0.995), outside())
    assert v is not None
    assert v.principle == 16


def test_calibration_hygiene_is_advisory_not_blocking():
    """P16 stopped being a gate: it flags an extreme, it does not send it back.

    The old check blocked, and a retry could satisfy it by lowering its own confidence
    label and retreating the probability — neither of which is new evidence.
    """
    v = checks.check_calibration_hygiene(forecast(0.995), outside())
    assert v is not None
    assert v.blocking is False


def test_calibration_hygiene_allows_argued_extreme():
    """An extreme is justified, not forbidden — writing the argument is what clears it."""
    o = outside(lenses=[ref("a", 0.97), ref("b", 0.99)], aggregate_base_rate=0.98)
    f = forecast(
        0.99, extreme_justification="class 'b' carries it; n=40 and both agree"
    )
    assert checks.check_calibration_hygiene(f, o) is None


def test_calibration_hygiene_flags_retreat_to_the_boundary():
    """The regression this whole change exists for.

    A failing attempt used to pass by moving the probability to exactly the floor while
    *lowering* its confidence. The band check was `floor <= p <= ceiling`, so landing on
    the boundary skipped the earned-extreme test entirely and nothing about the evidence
    had changed.
    """
    o = outside(
        lenses=[ref("a", 0.05), ref("b", 0.35)],
        aggregate_base_rate=0.20,
        disagreement="explained",
    )
    v = checks.check_calibration_hygiene(forecast(0.02), o)
    assert v is not None
    assert v.principle == 16
    assert "edge" in v.detail


def test_calibration_hygiene_allows_the_boundary_when_classes_agree():
    """Sitting at the edge is only suspect when the outside view is itself unsure."""
    o = outside(lenses=[ref("a", 0.02), ref("b", 0.04)], aggregate_base_rate=0.03)
    assert checks.check_calibration_hygiene(forecast(0.02), o) is None


def test_calibration_bounds_are_configurable(monkeypatch):
    monkeypatch.setenv("CHECK_CALIBRATION_CEILING", "0.999")
    assert checks.check_calibration_hygiene(forecast(0.995), outside()) is None


# ---------- P1: sub-question linkage ----------


def ided(*ids: str) -> Decomposition:
    return decomposition(
        sub_questions=[
            sub().model_copy(update={"id": i}) for i in ids or ("sq1", "sq2", "sq3")
        ]
    )


def test_linkage_accepts_references_to_real_sub_questions():
    d = ided("sq1", "sq2", "sq3")
    o = outside(lenses=[ref("a", 0.20), ref("b", 0.24)])
    o.lenses[0].sub_question_ids = ["sq1"]
    f = forecast().model_copy(update={"decompositions": d.sub_questions})
    assert checks.check_linkage(f, d, o, inside()) is None


def test_linkage_catches_an_invented_sub_question_id():
    d = ided("sq1", "sq2", "sq3")
    o = outside(lenses=[ref("a", 0.20), ref("b", 0.24)])
    o.lenses[0].sub_question_ids = ["sq9"]
    f = forecast().model_copy(update={"decompositions": d.sub_questions})
    v = checks.check_linkage(f, d, o, inside())
    assert v is not None
    assert "sq9" in v.detail


def test_linkage_catches_synthesis_dropping_a_sub_question():
    """Synthesis regenerates `Forecast.decompositions`; every link dangles if it drifts."""
    d = ided("sq1", "sq2", "sq3")
    # Deep copies: a slice would alias the decomposition's own objects, so renaming one
    # would rename it on both sides and the check would have nothing to find.
    carried = [s.model_copy(deep=True) for s in d.sub_questions]
    carried[2] = carried[2].model_copy(update={"id": "renamed"})
    f = forecast().model_copy(update={"decompositions": carried})
    v = checks.check_linkage(f, d, outside(), inside())
    assert v is not None
    assert "points at nothing" in v.detail


def test_linkage_allows_a_class_that_addresses_the_whole_question():
    d = ided("sq1", "sq2", "sq3")
    f = forecast().model_copy(update={"decompositions": d.sub_questions})
    assert checks.check_linkage(f, d, outside(), inside()) is None


# ---------- P7: base-rate aggregation ----------


def test_aggregation_accepts_an_honestly_weighted_anchor():
    o = outside(
        lenses=[ref("a", 0.10, weight=0.25), ref("b", 0.90, weight=0.75)],
        aggregate_base_rate=0.70,
        disagreement="explained",
    )
    assert checks.check_aggregation(o) is None


def test_aggregation_rejects_an_anchor_the_weights_do_not_support():
    """The hole this check fills: the anchor used to be an unverifiable blend."""
    o = outside(
        lenses=[ref("a", 0.10, weight=0.5), ref("b", 0.90, weight=0.5)],
        aggregate_base_rate=0.85,
        disagreement="explained",
    )
    v = checks.check_aggregation(o)
    assert v is not None
    assert v.principle == 7
    assert "0.500" in v.detail


def test_aggregation_slack_is_configurable(monkeypatch):
    o = outside(
        lenses=[ref("a", 0.20, weight=1.0), ref("b", 0.30, weight=1.0)],
        aggregate_base_rate=0.35,
    )
    assert checks.check_aggregation(o) is not None
    monkeypatch.setenv("CHECK_AGGREGATE_SLACK", "0.15")
    assert checks.check_aggregation(o) is None


def test_a_weightless_lens_cannot_be_constructed():
    """The zero-weight case moved from a runtime check to the schema.

    `check_aggregation` used to report "no reference class carries any weight", because a
    weightless class could exist and made the blend undefined. `Lens.weight` is now
    `gt=0`, so the state is unreachable through the model and the remaining guard in
    `weighted_base_rate` is belt-and-braces for hand-built fixtures.
    """
    with pytest.raises(ValidationError):
        ref("a", 0.20, weight=0.0)


# ---------- P7: the anchor is the chain the decomposition describes ----------


def researched(sub_question_id: str, rate: float) -> ResearchedLens:
    """A lens that names exactly one sub-question, as the merge stamps them."""
    return ref(f"lens for {sub_question_id}", rate, sub_question_ids=[sub_question_id])


def a_grid(
    rule: str,
    rates: dict[str, float],
    estimates: dict[str, float],
    groups: tuple[DependentGroup, ...] = (),
):
    """A decomposition and an outside view sharing sub-question ids sq1..sqN."""
    ids = sorted(set(rates) | set(estimates))
    d = Decomposition(
        sub_questions=[
            SubPrediction(
                question=f"part {i}",
                probability=estimates.get(i, 0.5),
                rationale="because",
                knowability="researchable" if i in rates else "judgment",
            ).model_copy(update={"id": i})
            for i in ids
        ],
        chain_rule=rule,
        chain_note="stated",
        dependent_groups=list(groups),
    )
    o = OutsideView(
        lenses=[researched(i, r) for i, r in sorted(rates.items())]
        or [ref("a", 0.2), ref("b", 0.2)],
        aggregate_base_rate=0.0,
        disagreement="",
    )
    return d, o


def test_a_conjunction_anchor_is_the_product_of_its_columns():
    """A mean of conjunction factors is always >= their product. That gap was a
    systematic upward bias on every conjunctive question."""
    d, o = a_grid("conjunction", {"sq1": 0.55, "sq2": 0.70, "sq3": 0.60}, {})
    o.aggregate_base_rate = 0.55 * 0.70 * 0.60

    assert checks.check_aggregation(o, d) is None
    # The pre-3.3 arithmetic would have said 0.617 — more than 3x the truth.
    assert checks.weighted_base_rate(o) == pytest.approx(0.6167, abs=1e-3)


def test_a_disjunction_anchor_is_one_minus_the_product_of_complements():
    d, o = a_grid("disjunction", {"sq1": 0.20, "sq2": 0.30, "sq3": 0.10}, {})
    o.aggregate_base_rate = 1 - (0.80 * 0.70 * 0.90)

    assert checks.check_aggregation(o, d) is None


def test_a_column_nobody_researched_contributes_its_own_estimate():
    """The empty-cell trap. Skipping sq4 silently treats it as certain."""
    d, o = a_grid("conjunction", {"sq1": 0.55, "sq2": 0.70, "sq3": 0.60}, {"sq4": 0.80})

    rows = checks.chain_inputs(d, o)
    assert [r["source"] for r in rows] == ["researched"] * 3 + ["estimated"]
    assert rows[-1]["rate"] == pytest.approx(0.80)

    implied, rule = checks.anchor_from(o, d)
    assert rule == "conjunction"
    assert implied == pytest.approx(0.55 * 0.70 * 0.60 * 0.80)
    # Without sq4 the product would be 0.231 — a quarter higher than the truth.
    assert implied < 0.55 * 0.70 * 0.60


def test_custom_falls_back_to_the_weighted_mean():
    """No formula to apply, so the pre-3.3 arm is the honest answer."""
    d, o = a_grid("custom", {"sq1": 0.20, "sq2": 0.24, "sq3": 0.22}, {})
    o.aggregate_base_rate = 0.22

    implied, rule = checks.anchor_from(o, d)
    assert rule == "weighted mean"
    assert implied == pytest.approx(0.22)
    assert checks.check_aggregation(o, d) is None


def test_no_decomposition_keeps_the_pre_3_3_behaviour():
    """`d` is optional so the component evals and direct unit tests need no edit."""
    o = outside(lenses=[ref("a", 0.20), ref("b", 0.24)], aggregate_base_rate=0.22)
    assert checks.check_aggregation(o) is None
    assert checks.check_aggregation(o, None) is None


def test_aggregation_catches_an_anchor_that_is_not_the_chain():
    d, o = a_grid("conjunction", {"sq1": 0.55, "sq2": 0.70, "sq3": 0.60}, {})
    o.aggregate_base_rate = 0.62  # the mean, not the product

    v = checks.check_aggregation(o, d)
    assert v is not None
    assert "conjunction" in v.detail


# ---------- P1: sub-questions that move together ----------


def group(*members: int, kind: str = "shared_driver") -> DependentGroup:
    return DependentGroup(name="they move together", members=list(members), kind=kind)


def test_a_decomposition_with_no_dependent_groups_anchors_exactly_where_it_did_before():
    """ADR 28. A checkpoint written before this field existed has to load and produce
    the number it produced then, not merely a close one."""
    old = {
        "sub_questions": [
            {"question": f"part {i}", "probability": 0.5, "rationale": "because"}
            for i in range(3)
        ],
        "chain_rule": "conjunction",
        "chain_note": "stated",
    }
    d = Decomposition.model_validate(old)
    assert d.dependent_groups == []

    rates = [0.55, 0.70, 0.60]
    assert checks.combine_sub_question_rates(rates, "conjunction") == 0.55 * 0.70 * 0.60


def test_a_shared_driver_group_pulls_a_conjunction_up_toward_its_weakest_member():
    """Correlated parts do not multiply. Multiplying them understates the answer."""
    rates = [0.5, 0.8, 0.6]
    combined = checks.combine_sub_question_rates(rates, "conjunction", [group(1, 2)])

    assert combined == pytest.approx(0.261)
    assert combined > math.prod(rates)  # 0.240 if they were independent
    assert combined < min(rates)  # but never past the Fréchet-Hoeffding bound


def test_a_shared_driver_group_pulls_a_disjunction_down_toward_its_strongest_member():
    """The direction flips. Two things that fire together give an OR fewer distinct
    chances to fire, so correlation makes a disjunction *less* likely."""
    rates = [0.2, 0.3, 0.1]
    combined = checks.combine_sub_question_rates(rates, "disjunction", [group(1, 2)])

    assert combined == pytest.approx(0.4519)
    assert combined < 1 - (0.8 * 0.7 * 0.9)  # 0.496 if they were independent
    assert combined > max(rates)


def test_a_group_of_kind_none_changes_nothing():
    """A dependence parameter of 0 is the identity. This catches an off-by-one in the
    partition, which would otherwise look like a plausible number."""
    rates = [0.5, 0.8, 0.6]
    assert checks.combine_sub_question_rates(
        rates, "conjunction", [group(1, 2, kind="none")]
    ) == pytest.approx(math.prod(rates))


def test_a_group_naming_a_sub_question_that_does_not_exist_is_rejected():
    """The delete-then-save case. Positions shift; a stale group must not silently
    point at a different sub-question."""
    with pytest.raises(ValidationError, match="names sub-question 4 of 3"):
        a_grid("conjunction", {"sq1": 0.5, "sq2": 0.8, "sq3": 0.6}, {}, (group(1, 4),))


def test_a_sub_question_cannot_sit_in_two_groups():
    """Without this the grouped and ungrouped sets stop partitioning the rates, and
    sq2 is counted twice."""
    with pytest.raises(ValidationError, match="more than one group"):
        a_grid(
            "conjunction",
            {"sq1": 0.5, "sq2": 0.8, "sq3": 0.6},
            {},
            (group(1, 2), group(2, 3)),
        )


def test_a_custom_chain_rule_cannot_carry_dependent_groups():
    """`custom` has no formula, so there is nothing for a dependence parameter to move.
    Rejecting says so; accepting would leave a field on screen that changes nothing."""
    with pytest.raises(ValidationError, match="no formula for dependence"):
        a_grid("custom", {"sq1": 0.5, "sq2": 0.8, "sq3": 0.6}, {}, (group(1, 2),))


def test_the_anchor_and_the_implied_probability_apply_the_same_groups():
    """ADR 33's property. If only one of the two read `dependent_groups`, the anchor and
    `check_derivation` would quietly disagree about what the evidence implies."""
    d, o = a_grid(
        "conjunction", {"sq1": 0.5, "sq2": 0.8, "sq3": 0.6}, {}, (group(1, 2),)
    )
    anchor, rule = checks.anchor_from(o, d)

    assert rule == "conjunction"
    assert anchor == pytest.approx(0.261)

    i = inside(adjustments=[adjustment("neutral", 0.0)])
    assert checks.implied_probability(o, i, d) == pytest.approx(anchor)


# ---------- P7: spread is measured within a column ----------


def test_dragonfly_ignores_spread_between_different_columns():
    """The regression this rescoping exists to prevent.

    A 0.15 lens on one sub-question and a 0.80 lens on another are not disagreeing —
    they are measuring different things. Whole-view spread called that 0.65.
    """
    o = outside(
        lenses=[researched("sq1", 0.15), researched("sq4", 0.80)],
        disagreement="",
    )
    assert checks.base_rate_spread(o) == pytest.approx(0.65)
    assert checks.check_dragonfly(o) is None


def test_dragonfly_still_fires_within_one_column():
    o = outside(
        lenses=[researched("sq1", 0.12), researched("sq1", 0.55)],
        disagreement="",
    )
    v = checks.check_dragonfly(o)
    assert v is not None
    assert "for sq1" in v.detail


def test_the_worst_column_is_the_one_reported():
    o = outside(
        lenses=[
            researched("sq1", 0.20),
            researched("sq1", 0.24),
            researched("sq2", 0.10),
            researched("sq2", 0.70),
        ],
        disagreement="",
    )
    assert checks.sub_question_spreads(o) == {
        "sq1": pytest.approx(0.04),
        "sq2": pytest.approx(0.60),
    }
    assert checks.worst_sub_question_spread(o) == pytest.approx(0.60)
    v = checks.check_dragonfly(o)
    assert v is not None and "for sq2" in v.detail


# ---------- Citations ----------


def test_citations_pass_when_every_url_was_retrieved():
    o = outside(
        lenses=[
            ref("a", 0.20, sources=[graded(url="https://x.test/a")]),
            ref("b", 0.24),
        ]
    )
    seen = [SourceRef(url="https://x.test/a", tool="search_web")]
    assert checks.check_citations(o, inside(), seen) is None


def test_citations_catch_a_url_that_was_never_fetched():
    o = outside(
        lenses=[
            ref("a", 0.20, sources=[graded(url="https://invented.test/report")]),
            ref("b", 0.24),
        ]
    )
    v = checks.check_citations(o, inside(), [])
    assert v is not None
    assert "invented.test" in v.detail


def test_citations_ignore_sources_with_no_url():
    """A paywalled dataset is a legitimate source; only a fabricated link is a lie."""
    assert checks.check_citations(outside(), inside(), []) is None


@pytest.mark.parametrize(
    "bad",
    [
        "/goto?url=CAESzAEB7keqTahb",  # a redirect fragment, not a link
        "goto?url=abc",
        "javascript:alert(1)",
        "   ",
        "not a url at all",
    ],
)
def test_graded_source_drops_a_url_that_is_not_absolute_http(bad):
    """A relative href resolves against our own origin — a dead link that looks real.

    Search results reach the model as prose, so what comes back is whatever it copied.
    """
    assert GradedSource(source="x", url=bad, confidence="high", note="n").url is None


def test_graded_source_keeps_a_real_url():
    s = GradedSource(
        source="x", url="https://example.test/a?b=c", confidence="high", note="n"
    )
    assert s.url == "https://example.test/a?b=c"


# ---------- Source confidence ----------


def test_claim_support_takes_the_strongest_source():
    """Max, not mean — citing extra weak corroboration must not downgrade a claim.

    Averaging would teach the agent to cite less, which is the opposite of the point.
    """
    assert checks.claim_support([graded(confidence="high")]) == "high"
    assert (
        checks.claim_support([graded(confidence="high"), graded(confidence="low")])
        == "high"
    )


def test_claim_support_with_no_sources_is_low():
    assert checks.claim_support([]) == "low"


def test_aggregate_source_confidence_is_weighted_by_fit_and_magnitude():
    strong = outside(
        lenses=[
            ref("a", 0.20, weight=1.0, sources=[graded(confidence="high")]),
            ref("b", 0.24, weight=1.0, sources=[graded(confidence="high")]),
        ]
    )
    i = inside(
        adjustments=[adjustment("up", 0.10, sources=[graded(confidence="high")])]
    )
    assert checks.aggregate_source_confidence(strong, i) == "high"

    thin = outside(
        lenses=[
            ref("a", 0.20, weight=1.0, sources=[graded(confidence="low")]),
            ref("b", 0.24, weight=1.0, sources=[graded(confidence="low")]),
        ]
    )
    assert checks.aggregate_source_confidence(thin, i) == "medium"


def test_aggregate_source_confidence_skips_noise_adjustments():
    """Noise contributes zero to the probability, so it must not drag the grade."""
    o = outside(
        lenses=[
            ref("a", 0.20, sources=[graded(confidence="high")]),
            ref("b", 0.24, sources=[graded(confidence="high")]),
        ]
    )
    noisy = inside(
        adjustments=[
            adjustment("up", 0.10, sources=[graded(confidence="high")]),
            adjustment("up", 0.0, is_noise=True, sources=[]),
        ]
    )
    assert checks.aggregate_source_confidence(o, noisy) == "high"


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
        lenses=[ref("a", 0.10), ref("b", 0.60)],
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
    v_block = checks.check_dragonfly(outside(lenses=[ref("a", 0.10), ref("b", 0.60)]))
    assert v_block is not None
    v_soft = v_block.model_copy(update={"blocking": False})
    assert checks.blocking([v_block, v_soft]) == [v_block]


def test_thresholds_default_when_none_passed():
    """Every check accepts t=None and reads config itself."""
    assert checks.check_dragonfly(outside(), None) is None
    assert get_check_thresholds().reference_class_disagreement == 0.20
