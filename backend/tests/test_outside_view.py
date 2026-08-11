"""The outside view's integrity rule: a cell does not get to name its own population.

Lenses are chosen blind, before anything is looked up, so that a cell cannot settle on
whichever population gave the answer it already liked (ADR 40). The rule is enforced by
re-imposing the chosen lens's identity and weight on whatever the cell returns.

`stages.run_base_rate_step` does that for the main path. `_whole_question_cell` runs the
same cell without going through it, and used to keep whatever came back — so the one path
with no oversight was also the only one where the rule did not apply. That is what these
tests pin.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from superforecaster.agents import outside_view
from superforecaster.deps import ForecastDeps
from superforecaster.models import (
    Evidence,
    ForecastInput,
    GradedSource,
    ResearchedLens,
    SubQuestionBaseRates,
)

from .gated_factories import decomposition


def _input() -> ForecastInput:
    return ForecastInput(
        question="Will it happen?",
        resolution_criteria="It observably happens.",
        resolution_date=datetime(2027, 1, 1, tzinfo=timezone.utc),
        category="general",
    )


def _a_cell_that_measured_something_else() -> SubQuestionBaseRates:
    """What a cell returns when it ignores the population it was handed."""
    return SubQuestionBaseRates(
        lens=ResearchedLens(
            name="a population I liked better",
            population="something wider that was easier to find",
            why_it_fits="it was easier to find",
            weight=0.2,
            weight_rationale="I decided this after measuring it",
            evidence=[
                Evidence(
                    kind="published",
                    hits=24,
                    n=40,
                    note="24 of 40",
                    source=GradedSource(
                        source="an index", confidence="medium", note="adjacent"
                    ),
                )
            ],
            sub_question_ids=["sq9"],
        ),
        disagreement="not the population I was asked for",
    )


async def test_the_fallback_cell_reports_the_population_it_was_given(monkeypatch):
    """The identity and weight come from the synthetic whole-question lens, never from
    what came back — the same six fields `stages.run_base_rate_step` re-imposes."""

    async def fake_cell(input, sub_question, lens, deps):
        return _a_cell_that_measured_something_else()

    monkeypatch.setattr(outside_view, "run_research_lens", fake_cell)

    view = await outside_view._whole_question_cell(
        _input(), decomposition(), ForecastDeps()
    )

    measured = view.lenses[0]
    assert measured.name == "the question as a whole"
    assert measured.population.startswith("Cases comparable to:")
    assert measured.weight == 1.0
    assert measured.weight_rationale == "The only lens available."
    assert measured.sub_question_ids == []


async def test_the_fallback_cell_keeps_the_evidence_it_measured(monkeypatch):
    """Only identity and weight are re-imposed. The counted evidence is the one thing the
    cell is actually for, and re-imposing that would discard the work."""

    async def fake_cell(input, sub_question, lens, deps):
        return _a_cell_that_measured_something_else()

    monkeypatch.setattr(outside_view, "run_research_lens", fake_cell)

    view = await outside_view._whole_question_cell(
        _input(), decomposition(), ForecastDeps()
    )

    assert [(e.hits, e.n) for e in view.lenses[0].evidence] == [(24, 40)]
    assert view.disagreement == "not the population I was asked for"


async def test_every_lens_failing_is_an_error_rather_than_an_invented_anchor():
    """A run with nothing to stand on says so. An anchor made up from no evidence would be
    worse than an error, because nothing downstream could tell the difference."""
    with pytest.raises(RuntimeError, match="every lens failed"):
        await outside_view.whole_question_outside(
            _input(), decomposition(), ForecastDeps(), ["boom"]
        )
