"""Shared factories for gated-run tests. Not a test module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from superforecaster.models import (
    ALL_BIASES,
    Adjustment,
    BaseRateStepPayload,
    BiasCheck,
    CheckViolation,
    Decomposition,
    Evidence,
    Forecast,
    GradedSource,
    InsideStepPayload,
    InsideView,
    Lens,
    OutsideView,
    Reflection,
    ResearchedLens,
    ResearchSummary,
    SubQuestionLenses,
    SubPrediction,
    SynthesisStepPayload,
)


def future(days: int = 60) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def sub(id: str = "sq1", knowability: str = "researchable") -> SubPrediction:
    return SubPrediction(
        id=id,
        question=f"Sub-question {id}?",
        probability=0.5,
        rationale="because",
        knowability=knowability,
    )


def decomposition(
    knowabilities: tuple[str, ...] = ("researchable", "judgment", "researchable"),
) -> Decomposition:
    return Decomposition(
        sub_questions=[
            sub(id=f"sq{i + 1}", knowability=k) for i, k in enumerate(knowabilities)
        ],
        chain_rule="conjunction",
        chain_note="all must hold",
    )


def lens(name: str = "lens-a") -> Lens:
    return Lens(
        name=name,
        population=f"cases in {name}",
        why_it_fits="closest available population",
        weight=1.0,
        weight_rationale="only one",
    )


def chosen_lenses(*names: str) -> SubQuestionLenses:
    return SubQuestionLenses(lenses=[lens(n) for n in (names or ("lens-a",))])


def researched(name: str = "lens-a", sub_question_id: str = "sq1") -> ResearchedLens:
    return ResearchedLens(
        name=name,
        population=f"cases in {name}",
        why_it_fits="closest available population",
        weight=1.0,
        weight_rationale="only one",
        evidence=[
            Evidence(
                kind="published",
                hits=20,
                n=100,
                note="20 of 100",
                source=GradedSource(
                    source="dataset", confidence="high", note="direct"
                ),
            )
        ],
        sub_question_ids=[sub_question_id],
    )


def base_rate_payload(
    name: str = "lens-a", sub_question_id: str = "sq1"
) -> BaseRateStepPayload:
    return BaseRateStepPayload(
        lens=researched(name, sub_question_id), disagreement="none"
    )


def inside_payload(name: str = "lens-a", sub_question_id: str = "sq1") -> InsideStepPayload:
    return InsideStepPayload(
        lens_name=name,
        adjustments=[
            Adjustment(
                title="recent shift favours it",
                evidence="a recent shift",
                direction="up",
                magnitude=0.05,
                flip_test="the opposite would move it down",
                is_noise=False,
                lens_name=name,
                sub_question_ids=[sub_question_id],
            )
        ],
        steel_man="it might not hold",
    )


def reflection() -> Reflection:
    return Reflection(
        steel_man="the strongest case against",
        what_would_change_my_mind="a contrary dataset",
        bias_checks=[BiasCheck(bias=b, assessment="checked") for b in ALL_BIASES],
    )


def forecast(probability: float = 0.25) -> Forecast:
    return Forecast(
        question="Will it happen?",
        resolution_criteria="It observably happens.",
        resolution_date=future(),
        category="test",
        probability=probability,
        decompositions=decomposition().sub_questions,
        research=ResearchSummary(),
        reasoning="Because the chain implies it.",
    )


def synthesis_payload() -> SynthesisStepPayload:
    outside = OutsideView(
        lenses=[researched()],
        aggregate_base_rate=0.2,
        disagreement="",
    )
    inside = InsideView(
        adjustments=inside_payload().adjustments,
        steel_man="against",
        what_would_change_my_mind="data",
        bias_checks=reflection().bias_checks,
    )
    return SynthesisStepPayload(
        reflection=reflection(),
        outside=outside,
        inside=inside,
        forecast=forecast(),
        violations=[
            CheckViolation(
                principle=6, name="derivation", detail="drifted", blocking=False
            )
        ],
        anchor=0.2,
        implied=0.25,
        derivation_slack=0.05,
        attempts=1,
    )
