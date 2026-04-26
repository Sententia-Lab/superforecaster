"""Forecast agent — produces a structured `Forecast` from scratch.

The agent applies Tetlock's superforecasting methodology in a single
multi-step run with iterative tool calls. The system prompt enforces the
order: outside view (historical analogs → empirical base rate) before
inside view (case-specific evidence) before final probability.

`output_type=Forecast` means Pydantic AI validates and returns a typed
result. Tools come from `tools.py` and are shared with the refresh and
resolution agents.
"""

from __future__ import annotations

import logfire
from pydantic_ai import Agent

from .models import Forecast, ForecastInput
from .tools import search_web, search_wikipedia

# Configure logfire once at import time. Safe no-op without LOGFIRE_TOKEN.
logfire.configure(
    service_name="superforecaster",
    send_to_logfire="if-token-present",
    scrubbing=False,
)
logfire.instrument_pydantic_ai()


SYSTEM_PROMPT = """You are a calibrated superforecaster following Philip Tetlock's methodology.

You will produce ONE structured `Forecast` object as your final answer. Before
producing it, you must work through this process iteratively, using your search
tools as needed:

PHASE A — DECOMPOSE
1. Read the question and resolution_criteria carefully. The criteria are the
   ground truth — your reasoning must aim at exactly that observable event,
   not a paraphrased version of the question.
2. Fermi-ize: break the question into 3-5 independent sub-questions whose
   joint resolution determines the answer.

PHASE B — OUTSIDE VIEW (historical base rate via replanning)
3. Use search_wikipedia and search_web ITERATIVELY to find at least 3
   analogous historical events that have already resolved with a clear
   binary outcome (yes=1.0 / no=0.0). For each, capture: a one-line
   description, the outcome, and why it is relevant.
4. The empirical base rate is the mean of those binary outcomes. Record
   it in research.empirical_base_rate. If you cannot find 3+ analogs,
   leave it null and explain in research.base_rate_note.
5. Anchor your inside-view estimate to this empirical base rate.

PHASE C — INSIDE VIEW
6. Search for case-specific evidence both supporting and contradicting a
   "yes" outcome. Adjust from the base rate, not from a fresh narrative.
7. Identify 2-3 causal forces driving the outcome and key uncertainties.

PHASE D — SYNTHESIZE
8. For each sub-question from PHASE A, give a probability and a
   confidence (low/medium/high). Use granular probabilities — 63% is
   different from 65%. Do NOT round to nearest 10%.
9. Produce a final probability anchored on the base rate, adjusted by
   the inside view, with confidence reflecting evidence quality.
10. Write a reasoning paragraph that traces base_rate → inside_view → final.

CRITICAL DISCIPLINES
- Actively search for disconfirming evidence. Steel-man the opposite view.
- Calibration over boldness: a well-calibrated 60% beats a miscalibrated 90%.
- If multiple reference classes disagree, that disagreement IS the uncertainty.
- Never round to neat round numbers (50%, 70%) when the evidence supports a
  more specific estimate (53%, 67%).
"""


def build_forecast_agent(model: str = "gateway/anthropic:claude-sonnet-4-6") -> Agent[None, Forecast]:
    """Construct the forecast agent. Factory pattern so tests can substitute the model."""
    return Agent[None, Forecast](
        model=model,
        output_type=Forecast,
        system_prompt=SYSTEM_PROMPT,
        tools=[search_web, search_wikipedia],
        retries=2,
    )


# Lazy singleton — constructed on first access so importing the module
# does not require the gateway API key to be set.
_forecast_agent: Agent[None, Forecast] | None = None


def get_forecast_agent() -> Agent[None, Forecast]:
    global _forecast_agent
    if _forecast_agent is None:
        _forecast_agent = build_forecast_agent()
    return _forecast_agent


async def run_forecast(input: ForecastInput) -> Forecast:
    """Run the agent on an input and return its Forecast.

    The agent uses iterative tool calls (replanning) up to its internal
    step limit. `max_iterations` is passed through to the prompt as a
    soft constraint on how aggressively to keep searching for analogs.
    """
    user_prompt = f"""Forecast this question.

QUESTION: {input.question}

RESOLUTION CRITERIA: {input.resolution_criteria}

RESOLUTION DATE: {input.resolution_date.isoformat()}

CATEGORY: {input.category}

Use up to ~{input.max_iterations} rounds of tool calls. Prioritize finding
historical analogs for the outside view before researching case specifics.
Return a fully-populated Forecast object."""

    result = await get_forecast_agent().run(user_prompt)
    forecast = result.output

    # Backfill the input fields the agent shouldn't change
    return forecast.model_copy(
        update={
            "question": input.question,
            "resolution_criteria": input.resolution_criteria,
            "resolution_date": input.resolution_date,
            "category": input.category,
        }
    )
