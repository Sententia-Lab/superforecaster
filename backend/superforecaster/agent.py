"""Forecast agent — two-phase pipeline for guaranteed convergence.

Phase 1 (research): tool-using agent gathers evidence with a strict budget.
Phase 2 (synthesis): tool-free agent always produces the final Forecast.

Splitting research from synthesis prevents the model from searching indefinitely
while retrying structured-output validation on the full Forecast schema.
"""

from __future__ import annotations

import sys

import logfire
from config import get_research_limits, get_synthesis_limits, resolve_agent_model
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import UsageLimitExceeded

from .models import Forecast, ForecastInput, ForecastResearchNotes
from .observability import run_agent
from .tools import search_web, search_wikipedia

RESEARCH_SYSTEM_PROMPT = """You are a calibrated superforecaster in RESEARCH mode.

Your job is to gather evidence and draft decomposition — NOT to output a final
forecast probability. A separate synthesis step will do that.

PROCESS
1. Decompose the question into 3-5 sub-questions (Fermi-ize).
2. Use search_wikipedia and search_web to build an outside view:
   historical analogs with binary outcomes, empirical base rate if possible.
3. Search for case-specific supporting and contradicting evidence.
4. Record causal forces, uncertainties, and analysis_notes for the synthesizer.

RULES
- You have a strict search budget stated in the user prompt. Stop searching when
  it is exhausted, even if analogs are incomplete.
- Output ForecastResearchNotes only. Do NOT invent a final forecast probability.
- Prefer fewer, high-quality searches over exhaustive looping.
- If evidence is thin, say so in analysis_notes and base_rate_note.
"""

SYNTHESIS_SYSTEM_PROMPT = """You are a calibrated superforecaster in SYNTHESIS mode.

You receive research notes (and possibly raw search excerpts) from a prior phase.
You have NO search tools. Produce exactly one Forecast object now.

Use Tetlock's methodology:
- Anchor on the empirical base rate from research when present.
- Adjust with inside-view evidence; note gaps explicitly.
- Assign granular probabilities to each decomposition (not round numbers).
- Set final probability and confidence reflecting evidence quality.
- Reasoning must trace base_rate → inside_view → final.

If research was incomplete or budget-exhausted, still produce a best-effort
Forecast with appropriately wide uncertainty and low confidence.
"""


def build_research_agent(model: str | None = None) -> Agent[None, ForecastResearchNotes]:
    return Agent[None, ForecastResearchNotes](
        model=model or resolve_agent_model(),
        name="forecast_research_agent",
        output_type=ForecastResearchNotes,
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        tools=[search_web, search_wikipedia],
        retries=1,
    )


def build_synthesis_agent(model: str | None = None) -> Agent[None, Forecast]:
    return Agent[None, Forecast](
        model=model or resolve_agent_model(),
        name="forecast_synthesis_agent",
        output_type=Forecast,
        system_prompt=SYNTHESIS_SYSTEM_PROMPT,
        tools=[],
        retries=1,
    )


_research_agent: Agent[None, ForecastResearchNotes] | None = None
_synthesis_agent: Agent[None, Forecast] | None = None


def get_research_agent() -> Agent[None, ForecastResearchNotes]:
    global _research_agent
    if _research_agent is None:
        _research_agent = build_research_agent()
    return _research_agent


def get_synthesis_agent() -> Agent[None, Forecast]:
    global _synthesis_agent
    if _synthesis_agent is None:
        _synthesis_agent = build_synthesis_agent()
    return _synthesis_agent


def build_forecast_agent(model: str | None = None) -> Agent[None, Forecast]:
    """Backward-compatible factory — returns the synthesis agent."""
    return build_synthesis_agent(model)


def get_forecast_agent() -> Agent[None, Forecast]:
    """Backward-compatible accessor — returns the synthesis agent."""
    return get_synthesis_agent()


def _format_input_block(input: ForecastInput) -> str:
    return f"""QUESTION: {input.question}

RESOLUTION CRITERIA: {input.resolution_criteria}

RESOLUTION DATE: {input.resolution_date.isoformat()}

CATEGORY: {input.category}"""


def _messages_excerpt(messages: list, *, limit: int = 8000) -> str:
    parts: list[str] = []
    for msg in messages:
        role = type(msg).__name__
        content = getattr(msg, "parts", None) or [getattr(msg, "content", "")]
        for part in content:
            text = str(part)
            if len(text) > 1200:
                text = text[:1200] + "..."
            parts.append(f"{role}: {text}")
    excerpt = "\n".join(parts)
    if len(excerpt) > limit:
        return excerpt[:limit] + "\n...(truncated)"
    return excerpt


def _build_synthesis_prompt(
    input: ForecastInput,
    research: ForecastResearchNotes | None,
    raw_messages: list,
    *,
    research_exhausted: bool,
) -> str:
    research_json = (
        research.model_dump_json(indent=2)
        if research is not None
        else '{"note": "No structured research notes — research phase did not complete."}'
    )
    raw_excerpt = _messages_excerpt(raw_messages) if raw_messages else "(no captured messages)"
    budget_note = (
        "Research hit its usage budget before finishing. Use partial evidence below."
        if research_exhausted
        else "Research phase completed within budget."
    )
    return f"""Synthesize the final Forecast for this question.

{ _format_input_block(input) }

RESEARCH STATUS: {budget_note}

STRUCTURED RESEARCH NOTES (JSON):
{research_json}

RAW RESEARCH TRANSCRIPT (tool calls / results):
{raw_excerpt}

Produce a complete Forecast object. Fill question, resolution_criteria,
resolution_date, and category from the input above exactly.
"""


async def run_forecast(input: ForecastInput, *, verbose: bool = False) -> Forecast:
    """Run research (bounded) then synthesis (guaranteed output attempt)."""
    research_prompt = f"""Research this forecast question.

{_format_input_block(input)}

SEARCH BUDGET: at most {input.max_iterations} rounds. Each round = up to one
search_wikipedia and/or one search_web call. When the budget is used, stop
searching immediately and return ForecastResearchNotes with whatever you found."""

    research_notes: ForecastResearchNotes | None = None
    captured_messages: list = []
    research_exhausted = False

    with capture_run_messages() as messages:
        try:
            result = await run_agent(
                get_research_agent(),
                research_prompt,
                verbose=verbose,
                usage_limits=get_research_limits(input.max_iterations),
                run_name="forecast research",
            )
            research_notes = result.output
        except UsageLimitExceeded:
            research_exhausted = True
            logfire.warning(
                "forecast research hit usage limit; proceeding to synthesis",
                max_iterations=input.max_iterations,
                _tags=["agent-progress", "research-exhausted"],
            )
            if verbose:
                print(
                    "[agent] research budget exhausted — synthesizing from partial evidence",
                    file=sys.stderr,
                    flush=True,
                )
        captured_messages = list(messages)

    synthesis_prompt = _build_synthesis_prompt(
        input,
        research_notes,
        captured_messages,
        research_exhausted=research_exhausted,
    )
    synthesis_result = await run_agent(
        get_synthesis_agent(),
        synthesis_prompt,
        verbose=verbose,
        usage_limits=get_synthesis_limits(),
        run_name="forecast synthesis",
    )
    forecast = synthesis_result.output

    return forecast.model_copy(
        update={
            "question": input.question,
            "resolution_criteria": input.resolution_criteria,
            "resolution_date": input.resolution_date,
            "category": input.category,
        }
    )
