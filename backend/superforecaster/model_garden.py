"""Model registry keyed by training cutoff — clamp 2 of the two contamination clamps.

Clamping the tools (see `tools`) stops an agent citing a source published after the
question was asked. It does nothing about the model already knowing the answer: a
model trained through 2026 knows Russia invaded Ukraine in 2022 no matter what its
search tool returns.

The only fix is to run the question on a model whose training cutoff predates it.
`pick_clean_model` does that selection, and returns None rather than falling back to
a contaminated model — a skipped question is honest, a contaminated one is not.

Cutoffs come from Anthropic's published model documentation. They are never guessed
and never obtained by asking a model about itself, which models are unreliable about.
The docs distinguish "training data cutoff" (the broader range of data used) from
"reliable knowledge cutoff" (where knowledge is most extensive); this module uses the
*training* cutoff, which is the conservative choice, and stores the last day of the
stated month for the same reason.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import get_model_garden_margin_days, get_settings

from .models import ModelEntry

GARDEN_PATH = Path(__file__).resolve().parent / "model_garden.json"

_PROBE_PROMPT = "Reply with the single word: ok"


def load_garden(path: Path = GARDEN_PATH) -> list[ModelEntry]:
    """Read model_garden.json."""
    raw = json.loads(path.read_text())
    return [ModelEntry.model_validate(entry) for entry in raw]


def save_garden(entries: list[ModelEntry], path: Path = GARDEN_PATH) -> None:
    """Write the garden back, newest cutoff first."""
    ordered = sorted(entries, key=lambda e: e.training_cutoff, reverse=True)
    path.write_text(
        json.dumps([e.model_dump(mode="json") for e in ordered], indent=2) + "\n"
    )


def list_models(
    *, available_only: bool = True, path: Path = GARDEN_PATH
) -> list[ModelEntry]:
    """The garden, newest training cutoff first."""
    entries = load_garden(path)
    if available_only:
        entries = [e for e in entries if e.available]
    return sorted(entries, key=lambda e: e.training_cutoff, reverse=True)


def resolve_id(entry: ModelEntry) -> str:
    """Apply the Pydantic AI Gateway prefix when the deployment routes through it.

    The garden stores bare provider-qualified ids (`anthropic:claude-...`). A
    deployment using the gateway needs `gateway/anthropic:claude-...`, matching
    `config.resolve_agent_model()`.
    """
    if get_settings().pydantic_ai_gateway_api_key:
        return f"gateway/{entry.id}"
    return entry.id


def pick_clean_model(
    as_of: datetime | date,
    *,
    margin_days: int | None = None,
    path: Path = GARDEN_PATH,
) -> ModelEntry | None:
    """The most capable available model whose training predates `as_of`.

    Eligible means `training_cutoff + margin_days <= as_of`. "Most capable" means
    the newest eligible cutoff, on the assumption that a later cutoff tracks a
    better model.

    The margin exists because a published cutoff is approximate — data collection
    tapers rather than stops, so a model with a stated July cutoff may have seen
    some August text. MODEL_GARDEN_MARGIN_DAYS overrides the 90-day default.

    Returns None when nothing qualifies. Callers must skip the question; falling
    back to a contaminated model would produce a score that looks real and is not.
    """
    if margin_days is None:
        margin_days = get_model_garden_margin_days()

    asked = as_of.date() if isinstance(as_of, datetime) else as_of
    latest_allowed = asked - timedelta(days=margin_days)

    eligible = [
        e for e in list_models(path=path) if e.training_cutoff <= latest_allowed
    ]
    return eligible[0] if eligible else None


def earliest_cutoff(*, path: Path = GARDEN_PATH) -> date | None:
    """Oldest training cutoff currently available — the garden's reach.

    No question asked before this date plus the margin can be forecast cleanly.
    """
    entries = list_models(path=path)
    return min((e.training_cutoff for e in entries), default=None)


def coverage(
    asked_dates: list[datetime | date],
    *,
    margin_days: int | None = None,
    path: Path = GARDEN_PATH,
) -> tuple[int, int]:
    """How many of these questions have a clean model. Returns (covered, total)."""
    covered = sum(
        1
        for d in asked_dates
        if pick_clean_model(d, margin_days=margin_days, path=path) is not None
    )
    return covered, len(asked_dates)


async def probe(entry: ModelEntry) -> bool:
    """Send one trivial request to confirm the model is still served.

    Providers retire old models, and old models are exactly what this depends on —
    a garden entry that is no longer callable must not be picked.
    """
    from pydantic_ai import Agent
    from pydantic_ai.exceptions import UnexpectedModelBehavior, UserError

    try:
        agent = Agent(model=resolve_id(entry), output_type=str, retries=0)
        await agent.run(_PROBE_PROMPT)
    except (UserError, UnexpectedModelBehavior):
        return False
    except Exception:  # noqa: BLE001 — any provider error means "not usable"
        return False
    return True


async def probe_all(*, path: Path = GARDEN_PATH) -> list[ModelEntry]:
    """Probe every entry and rewrite the `available` flags in place."""
    entries = load_garden(path)
    probed = [
        entry.model_copy(update={"available": await probe(entry)}) for entry in entries
    ]
    save_garden(probed, path)
    return sorted(probed, key=lambda e: e.training_cutoff, reverse=True)


def render_garden(entries: list[ModelEntry]) -> str:
    """Plain-text table for the terminal."""
    if not entries:
        return "(garden is empty)"
    width = max(len(e.id) for e in entries)
    lines = [f"{'model'.ljust(width)}  cutoff      available"]
    for e in entries:
        mark = "yes" if e.available else "no"
        lines.append(f"{e.id.ljust(width)}  {e.training_cutoff.isoformat()}  {mark}")
    return "\n".join(lines)


def utc_today() -> date:
    return datetime.now(timezone.utc).date()
