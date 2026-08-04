"""Tests for clamp 2 — the model must not have been trained on the answer.

A bug here is silent in the worst way. `pick_clean_model` returning a too-new model
still produces a forecast, still produces a scorecard, and still looks green — while
the model recites an outcome it memorised during training. Nothing at runtime would
flag it, which is why the selection logic is tested against a fixture garden rather
than trusted.

The "returns None rather than falling back" property is the important one: a skipped
question is honest, a contaminated one is a number that looks real and is not.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from superforecaster import model_garden as mg
from superforecaster.models import ModelEntry

# A fixture garden with three tiers of cutoff, so "newest eligible" has a real answer.
FIXTURE = [
    {
        "id": "p:new",
        "provider": "p",
        "training_cutoff": "2026-01-31",
        "available": True,
    },
    {
        "id": "p:mid",
        "provider": "p",
        "training_cutoff": "2025-07-31",
        "available": True,
    },
    {
        "id": "p:old",
        "provider": "p",
        "training_cutoff": "2024-01-31",
        "available": True,
    },
    {
        "id": "p:retired",
        "provider": "p",
        "training_cutoff": "2020-01-31",
        "available": False,
    },
]


@pytest.fixture
def garden(tmp_path):
    path = tmp_path / "garden.json"
    path.write_text(json.dumps(FIXTURE))
    return path


# ---------- selection ----------


def test_picks_the_newest_eligible_model(garden):
    """Later cutoff tracks a better model, so prefer it among the eligible."""
    entry = mg.pick_clean_model(date(2026, 6, 1), margin_days=0, path=garden)
    assert entry is not None
    assert entry.id == "p:new"


def test_excludes_models_whose_cutoff_is_after_the_question(garden):
    """A model trained through Jan 2026 must not forecast an Aug 2025 question."""
    entry = mg.pick_clean_model(date(2025, 8, 1), margin_days=0, path=garden)
    assert entry is not None
    assert entry.id == "p:mid"


def test_returns_none_rather_than_a_contaminated_fallback(garden):
    """The property the whole clamp rests on — no silent downgrade."""
    assert mg.pick_clean_model(date(2023, 1, 1), margin_days=0, path=garden) is None


def test_ignores_unavailable_entries(garden):
    """p:retired has an ancient cutoff and would win if availability were ignored."""
    assert mg.pick_clean_model(date(2021, 1, 1), margin_days=0, path=garden) is None


def test_margin_pushes_the_boundary_back(garden):
    """A published cutoff is approximate, so the margin is a real exclusion."""
    just_after = date(2025, 8, 5)
    assert mg.pick_clean_model(just_after, margin_days=0, path=garden).id == "p:mid"
    assert mg.pick_clean_model(just_after, margin_days=90, path=garden).id == "p:old"


def test_margin_comes_from_env_when_not_passed(garden, monkeypatch):
    monkeypatch.setenv("MODEL_GARDEN_MARGIN_DAYS", "0")
    assert mg.pick_clean_model(date(2025, 8, 5), path=garden).id == "p:mid"

    monkeypatch.setenv("MODEL_GARDEN_MARGIN_DAYS", "90")
    assert mg.pick_clean_model(date(2025, 8, 5), path=garden).id == "p:old"


def test_accepts_a_datetime_as_well_as_a_date(garden):
    """Golden questions carry datetimes; the garden stores dates."""
    stamp = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc)
    assert mg.pick_clean_model(stamp, margin_days=0, path=garden).id == "p:new"


def test_boundary_is_inclusive(garden):
    """cutoff == as_of is eligible: training ended the day the question was asked."""
    assert (
        mg.pick_clean_model(date(2025, 7, 31), margin_days=0, path=garden).id == "p:mid"
    )
    assert (
        mg.pick_clean_model(date(2025, 7, 30), margin_days=0, path=garden).id == "p:old"
    )


# ---------- listing and reach ----------


def test_list_models_is_newest_cutoff_first(garden):
    assert [e.id for e in mg.list_models(path=garden)] == ["p:new", "p:mid", "p:old"]


def test_list_models_can_include_unavailable(garden):
    ids = [e.id for e in mg.list_models(available_only=False, path=garden)]
    assert "p:retired" in ids


def test_earliest_cutoff_reports_the_garden_reach(garden):
    """How far back a clean backtest can possibly go."""
    assert mg.earliest_cutoff(path=garden) == date(2024, 1, 31)


def test_coverage_counts_questions_with_a_clean_model(garden):
    asked = [date(2026, 6, 1), date(2025, 8, 1), date(2023, 1, 1)]
    assert mg.coverage(asked, margin_days=0, path=garden) == (2, 3)


# ---------- gateway routing ----------


def test_resolve_id_is_bare_without_a_gateway_key(monkeypatch):
    monkeypatch.delenv("PYDANTIC_AI_GATEWAY_API_KEY", raising=False)
    entry = ModelEntry(
        id="anthropic:x", provider="anthropic", training_cutoff=date(2025, 1, 1)
    )
    assert mg.resolve_id(entry) == "anthropic:x"


def test_resolve_id_adds_the_gateway_prefix_when_routed_through_it(monkeypatch):
    """Must match config.resolve_agent_model()'s `gateway/...` form or the run 404s."""
    monkeypatch.setenv("PYDANTIC_AI_GATEWAY_API_KEY", "pylf_v1_us_test")
    entry = ModelEntry(
        id="anthropic:x", provider="anthropic", training_cutoff=date(2025, 1, 1)
    )
    assert mg.resolve_id(entry) == "gateway/anthropic:x"


# ---------- the real garden ----------


def test_shipped_garden_parses():
    entries = mg.load_garden()
    assert entries
    assert all(e.training_cutoff.year >= 2024 for e in entries)


def test_shipped_garden_reach_is_recorded_accurately():
    """spec4.md quotes this floor; if the garden changes, that doc is stale."""
    entries = mg.list_models(available_only=False)
    assert min(e.training_cutoff for e in entries) == date(2025, 7, 31)


def test_shipped_garden_cannot_reach_the_legacy_golden_set():
    """The finding that deferred the backtest to spec4.md, pinned as a test.

    Every one of the 66 legacy questions was asked between Sep 2020 and Sep 2024,
    which is before every served model's training cutoff. If a model with an older
    cutoff ever ships, this test fails and the deferral should be revisited.
    """
    entries = mg.load_garden()
    for e in entries:
        e.available = True
    newest_legacy_question = date(2024, 9, 1)
    assert min(x.training_cutoff for x in entries) > newest_legacy_question
