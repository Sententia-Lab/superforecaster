"""Tests for the component scorers.

The scorers are where "good output for this agent" is written down. They ship before
the data does, so they need to be right before any case is authored — otherwise the
first failing eval is ambiguous between "the agent is bad" and "the scorer is wrong".

Two scorers encode a judgment worth pinning:

- `score_resolution` weights a false positive as fatal. Closing a live forecast is
  irreversible; missing a resolved one costs a day.
- `score_postmortem` must NOT reward calling a missed forecast "flawed". A 70%
  forecast that resolved "no" with sound reasoning is `sound_process`; a scorer that
  penalised that would be teaching outcome bias, which is the exact failure
  principle 13 exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.evals import components as ce
from superforecaster.models import (
    ComponentCase,
    ComponentScore,
    CriteriaCritique,
    Decomposition,
    EvidenceItem,
    PostMortem,
    ResolutionCheckResult,
    UpdateDecision,
)
from tests.test_checks import (
    counted_ref,
    adjustment,
    all_bias_checks,
    forecast,
    inside,
    outside,
    ref,
    sub,
)

# ---------- decompose ----------


def test_decompose_scorer_passes_a_good_decomposition():
    d = Decomposition(sub_questions=[sub(), sub(), sub()], chain_note="multiply")
    s = ce.score_decompose(d, {"min_sub_questions": 3})
    assert s.passed


def test_decompose_scorer_fails_when_nothing_is_researchable():
    d = Decomposition(
        sub_questions=[sub(knowability="judgment") for _ in range(3)], chain_note="x"
    )
    s = ce.score_decompose(d, {})
    assert not s.passed
    assert s.assertions["has_researchable"] is False


def test_decompose_scorer_checks_expected_terms():
    d = Decomposition(sub_questions=[sub(), sub(), sub()], chain_note="x")
    assert ce.score_decompose(d, {"must_mention": ["deal"]}).assertions[
        "mentions_expected_terms"
    ]
    assert not ce.score_decompose(d, {"must_mention": ["nuclear"]}).assertions[
        "mentions_expected_terms"
    ]


# ---------- outside view ----------


def test_outside_view_scorer_compares_against_the_documented_rate():
    o = outside(
        reference_classes=[ref("a", 0.20), ref("b", 0.24)], aggregate_base_rate=0.22
    )
    assert ce.score_outside_view(o, {"true_base_rate": 0.25, "tolerance": 0.05}).passed
    assert not ce.score_outside_view(
        o, {"true_base_rate": 0.60, "tolerance": 0.05}
    ).assertions["rate_near_documented_truth"]


def test_outside_view_scorer_requires_every_lens_to_be_backed():
    """A published statistic with a blank source is an assertion.

    A *counted* lens is exempt: its audit is the analogs, which
    `check_base_rate_derivation` verifies separately.
    """
    lens = ref("a", 0.2)
    blanked = lens.model_copy(
        update={
            "evidence": [
                lens.evidence[0].model_copy(
                    update={
                        "source": lens.evidence[0].source.model_copy(
                            update={"source": "  "}
                        )
                    }
                )
            ]
        }
    )
    o = outside(lenses=[blanked, ref("b", 0.24)])
    assert not ce.score_outside_view(o, {}).assertions["every_lens_sourced"]


def test_outside_view_scorer_accepts_a_counted_lens_without_a_citation():
    o = outside(lenses=[counted_ref("a"), ref("b", 0.24)])
    assert ce.score_outside_view(o, {}).assertions["every_lens_sourced"]
    assert ce.score_outside_view(o, {}).assertions["rates_are_derived"]


# ---------- inside view ----------


def test_inside_view_scorer_wants_the_decisive_fact_used_and_the_noise_discarded():
    i = inside(
        adjustments=[
            adjustment("up", 0.10, evidence="the board approved the merger"),
            adjustment("down", 0.0, is_noise=True, evidence="the CEO wore a blue tie"),
        ]
    )
    s = ce.score_inside_view(
        i, {"decisive_fact": "board approved", "irrelevant_fact": "blue tie"}
    )
    assert s.assertions["found_decisive_fact"]
    assert s.assertions["discarded_irrelevant_fact"]


def test_inside_view_scorer_fails_when_noise_was_treated_as_signal():
    i = inside(
        adjustments=[
            adjustment("up", 0.10, evidence="the board approved the merger"),
            adjustment("up", 0.06, evidence="the CEO wore a blue tie"),
        ]
    )
    s = ce.score_inside_view(i, {"irrelevant_fact": "blue tie"})
    assert not s.assertions["discarded_irrelevant_fact"]


# ---------- critic ----------


def critique(is_resolvable: bool, what_changed="") -> CriteriaCritique:
    return CriteriaCritique(
        is_resolvable=is_resolvable,
        what_changed=what_changed,
        suggested_criteria="rewritten" if not is_resolvable else "",
    )


def test_critic_scorer_matches_the_label():
    assert ce.score_critic(critique(True), {"is_resolvable": True}).passed
    assert not ce.score_critic(critique(True), {"is_resolvable": False}).passed


def test_critic_scorer_wants_the_edit_named_on_a_bad_case():
    s = ce.score_critic(critique(False, ""), {"is_resolvable": False})
    assert not s.assertions["said_what_it_changed"]

    s = ce.score_critic(
        critique(False, "Replaced 'significant' with 'at least 10%'."),
        {"is_resolvable": False, "known_ambiguity": "significant"},
    )
    assert s.passed


# ---------- resolution ----------


def resolution(
    appears_resolved: bool, evidence: str | None = "a source"
) -> ResolutionCheckResult:
    return ResolutionCheckResult(
        appears_resolved=appears_resolved,
        confidence="medium",
        resolution_evidence=evidence,
        reasoning="r",
    )


def test_resolution_scorer_passes_a_correct_call():
    assert ce.score_resolution(resolution(True), {"appears_resolved": True}).passed
    assert ce.score_resolution(resolution(False), {"appears_resolved": False}).passed


def test_resolution_scorer_treats_a_false_positive_as_fatal():
    """Closing a live forecast is irreversible — this must never be a soft failure."""
    s = ce.score_resolution(resolution(True), {"appears_resolved": False})
    assert not s.passed
    assert s.assertions["no_false_positive"] is False
    assert "FALSE POSITIVE" in s.detail


def test_resolution_scorer_false_negative_is_a_plain_failure():
    """Missing a resolved forecast costs a day, not a forecast — no extra penalty."""
    s = ce.score_resolution(resolution(False), {"appears_resolved": True})
    assert not s.passed
    assert "no_false_positive" not in s.assertions


def test_resolution_scorer_requires_evidence_when_claiming_resolution():
    s = ce.score_resolution(resolution(True, evidence=""), {"appears_resolved": True})
    assert not s.assertions["cited_evidence"]


# ---------- update ----------


def decision(prior: float, posterior: float, confirming: bool = True) -> UpdateDecision:
    if prior == posterior:
        evidence = []
    else:
        strong, weak = (0.9, 0.2) if confirming else (0.2, 0.9)
        evidence = [
            EvidenceItem(fact="f", source="s", p_if_true=strong, p_if_false=weak)
        ]
    return UpdateDecision(
        evidence=evidence, prior=prior, posterior=posterior, reasoning="r"
    )


def test_update_scorer_checks_direction():
    assert ce.score_update(decision(0.5, 0.65), {"direction": "up"}).passed
    assert not ce.score_update(decision(0.5, 0.65), {"direction": "down"}).passed


def test_update_scorer_accepts_a_justified_no_move():
    """Most days there is no news. Not moving is a correct answer, not a failure."""
    assert ce.score_update(decision(0.5, 0.5), {"direction": "none"}).passed


def test_update_scorer_catches_a_self_contradicting_update():
    d = UpdateDecision(
        evidence=[EvidenceItem(fact="f", source="s", p_if_true=0.9, p_if_false=0.1)],
        prior=0.5,
        posterior=0.3,
        reasoning="r",
    )
    assert not ce.score_update(d, {"direction": "down"}).assertions["bayes_consistent"]


# ---------- postmortem ----------


def postmortem(verdict: str, process=None, noise=None) -> PostMortem:
    return PostMortem(
        process_errors=process or [],
        outcome_noise=noise or [],
        verdict=verdict,
        lesson="a lesson",
    )


def test_postmortem_scorer_rewards_calling_a_sound_miss_sound():
    """A 70% forecast that resolved 'no' with good reasoning is sound_process.

    This is the assertion that keeps the scorer from teaching outcome bias.
    """
    s = ce.score_postmortem(
        postmortem("sound_process", noise=["the low-probability branch occurred"]),
        {"verdict": "sound_process"},
    )
    assert s.passed


def test_postmortem_scorer_fails_a_sound_case_judged_flawed():
    s = ce.score_postmortem(
        postmortem("flawed_process", process=["should have known"]),
        {"verdict": "sound_process"},
    )
    assert not s.passed


def test_postmortem_scorer_rejects_invented_process_errors_on_a_sound_case():
    s = ce.score_postmortem(
        postmortem("sound_process", process=["vaguely could have been better"]),
        {"verdict": "sound_process"},
    )
    assert not s.assertions["did_not_invent_process_errors"]


def test_postmortem_scorer_wants_a_named_error_on_a_flawed_case():
    s = ce.score_postmortem(postmortem("flawed_process"), {"verdict": "flawed_process"})
    assert not s.assertions["named_a_process_error"]


# ---------- harness ----------


def test_every_agent_has_a_scorer():
    assert set(ce.AGENTS) == set(ce.SCORERS)


async def test_run_component_reports_zero_cases_without_failing():
    report = await ce.run_component("decompose")
    assert report.n == 0
    assert "0 cases" in ce.render_report(report)


async def test_run_component_rejects_an_unknown_agent():
    import pytest

    with pytest.raises(ValueError, match="unknown agent"):
        await ce.run_component("nonsense")


async def test_case_is_skipped_when_no_clean_model_exists(tmp_path):
    """A 2022 question has no clean model, so it must skip rather than score dirty."""
    case = ComponentCase(
        id="old",
        agent="decompose",
        input={},
        expect={},
        as_of=datetime(2022, 1, 1, tzinfo=timezone.utc),
    )
    score = await ce.run_case(case, mode="clean")
    assert isinstance(score, ComponentScore)
    assert score.skipped is not None
    assert "training cutoff" in score.skipped


def test_report_aggregates_assertion_pass_rates():
    scores = [
        ComponentScore(case_id="a", passed=True, assertions={"x": True, "y": True}),
        ComponentScore(case_id="b", passed=False, assertions={"x": True, "y": False}),
        ComponentScore(case_id="c", skipped="no clean model"),
    ]
    report = ce._build_report("decompose", scores)
    assert report.n == 3
    assert report.pass_rate == 0.5  # skipped cases are excluded from the denominator
    assert report.assertion_pass_rates == {"x": 1.0, "y": 0.5}
