"""One agent per methodology step. Each module defines `INSTRUCTIONS`, a module-level
`agent`, and a `run_<n>(...)` function that stages, tests, and evals call."""

from __future__ import annotations

from typing import Any

from pydantic_ai import RunContext

from ..config import get_settings
from ..deps import ForecastDeps
from ..models import ForecastRecord

_TAVILY_TOOLS = frozenset({"search_web", "extract_pages"})

GRADING = """Grade each source for how strongly it supports THIS claim, not its reputation:
  high    directly on point, from something that would know
  medium  relevant but indirect, dated, or partial
  low     a single report, a secondhand figure, or a number you inferred
Say why in `note`. `source` is a human label, never a bare URL; put the link in `url`
only when a tool returned that exact URL — a check rejects invented links."""


async def withdraw_tools(ctx: RunContext[ForecastDeps], tool_defs: list) -> list:
    """Drop the Tavily tools without a key, and every tool once the budget is spent."""
    if not get_settings().tavily_api_key:
        tool_defs = [t for t in tool_defs if getattr(t, "name", t) not in _TAVILY_TOOLS]
    budget = getattr(ctx.deps, "budget", None)
    if budget is not None and ctx.usage.tool_calls >= budget.tool_calls:
        return []
    return tool_defs


def format_question(input: Any) -> str:
    """The question block every stage prompt opens with."""
    return f"""QUESTION: {input.question}

RESOLUTION CRITERIA: {input.resolution_criteria}

RESOLUTION DATE: {input.resolution_date.isoformat()}

CATEGORY: {input.category}"""


def format_history(record: ForecastRecord, last: int = 10) -> str:
    """The most recent probability updates, newest last."""
    updates = record.updates[-last:]
    if not updates:
        return "(no updates yet)"
    skipped = len(record.updates) - len(updates)
    lines = [f"({skipped} earlier updates omitted)"] if skipped else []
    for u in updates:
        late = " [LATE]" if u.is_late else ""
        lines.append(
            f"- {u.created_at.date().isoformat()} p={u.probability:.3f}{late}: {u.reasoning}"
        )
    return "\n".join(lines)
