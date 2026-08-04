"""Tests for the pass-inclusive check suite.

The point of `run_forecast_checks_detailed` is that a UI can tell "this check ran and
passed" from "this check never ran". The regression that matters most is that adding
it changed nothing about what `run_forecast_checks` reports.
"""

from __future__ import annotations

from superforecaster import checks
from tests.test_graph_forecast import (
    a_decomposition,
    a_forecast,
    an_inside_view,
    an_outside_view,
)


def detailed(probability: float = 0.28):
    return checks.run_forecast_checks_detailed(
        a_forecast(probability), a_decomposition(), an_outside_view(), an_inside_view()
    )


def violations(probability: float = 0.28):
    return checks.run_forecast_checks(
        a_forecast(probability), a_decomposition(), an_outside_view(), an_inside_view()
    )


def test_returns_every_check_in_display_order():
    assert [r.name for r in detailed()] == [n for n, _, _ in checks.FORECAST_CHECK_LABELS]


def test_a_clean_forecast_passes_all_seven():
    results = detailed(0.28)
    assert all(r.passed for r in results)
    assert all(r.violation is None for r in results)


def test_a_failing_check_carries_its_violation_and_detail():
    results = detailed(0.95)
    failed = [r for r in results if not r.passed]

    assert [r.name for r in failed] == ["derivation"]
    assert failed[0].violation.principle == 6
    assert "0.950" in failed[0].violation.detail


def test_labels_name_the_principle_the_ui_shows():
    labels = {r.name: r.label for r in detailed()}
    assert labels["derivation"] == "P6 derivation"
    assert labels["bias_coverage"] == "P15 bias coverage"


def test_run_forecast_checks_is_exactly_the_failures():
    """The regression: the old suite's output must be unchanged."""
    for probability in (0.28, 0.95, 0.01):
        assert violations(probability) == [
            r.violation for r in detailed(probability) if r.violation
        ]


def test_passing_checks_carry_no_detail():
    """Documented limit — the validators only produce a message on failure, so the UI
    renders the check name alone on a pass rather than inventing one."""
    assert all(r.violation is None for r in detailed(0.28))


# ---------- the shared arithmetic ----------


def test_implied_probability_is_the_anchor_plus_signed_adjustments():
    outside, inside = an_outside_view(), an_inside_view()
    # 0.22 anchor, +0.10 and -0.04
    assert checks.implied_probability(outside, inside) == 0.28


def test_implied_probability_clamps_to_a_probability():
    outside, inside = an_outside_view(), an_inside_view()
    for a in inside.adjustments:
        a.direction, a.magnitude = "down", 0.5

    assert checks.implied_probability(outside, inside) == 0.0


def test_signed_adjustment_zeroes_noise_and_neutral():
    inside = an_inside_view()
    inside.adjustments[0].is_noise = True
    inside.adjustments[1].direction = "neutral"

    assert [checks.signed_adjustment(a) for a in inside.adjustments] == [0.0, 0.0]


# ---------- the material behind each verdict ----------


def evidence(probability: float = 0.28):
    return checks.check_evidence(
        a_forecast(probability), a_decomposition(), an_outside_view(), an_inside_view()
    )


def test_evidence_covers_every_check():
    assert set(evidence()) == {n for n, _, _ in checks.FORECAST_CHECK_LABELS}


def test_derivation_evidence_is_the_walk_the_check_performed():
    """The numbers shown must be the ones the verdict was reached on, not a re-derivation."""
    e = evidence(0.95)["derivation"]

    assert e["anchor"] == 0.22
    assert [round(w["delta"], 3) for w in e["walk"]] == [0.10, -0.04]
    assert e["implied"] == 0.28
    assert e["stated"] == 0.95
    assert round(e["drift"], 3) == 0.67
    assert e["drift"] > e["slack"]


def test_derivation_walk_marks_noise_as_contributing_nothing():
    inside = an_inside_view()
    inside.adjustments[0].is_noise = True
    e = checks.check_evidence(
        a_forecast(0.18), a_decomposition(), an_outside_view(), inside
    )["derivation"]

    assert e["walk"][0]["delta"] == 0.0
    assert e["walk"][0]["is_noise"] is True


def test_dragonfly_evidence_shows_the_rates_and_the_threshold():
    e = evidence()["dragonfly"]

    assert [c["base_rate"] for c in e["classes"]] == [0.20, 0.24]
    assert round(e["spread"], 3) == 0.04
    assert e["threshold"] == 0.20


def test_bias_evidence_names_what_was_missing():
    inside = an_inside_view()
    inside.bias_checks = inside.bias_checks[:2]
    e = checks.check_evidence(
        a_forecast(), a_decomposition(), an_outside_view(), inside
    )["bias_coverage"]

    assert len(e["assessed"]) == 2
    assert len(e["missing"]) == 3


def test_evidence_is_attached_to_each_result():
    by_name = {r.name: r for r in detailed(0.95)}
    assert by_name["derivation"].evidence["stated"] == 0.95
    # A passing check carries its material too — the point is to be able to argue with
    # a verdict, not only to explain a failure.
    assert by_name["dragonfly"].evidence["classes"]
