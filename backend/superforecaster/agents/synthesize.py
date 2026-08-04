"""Synthesis agent — principles 6, 8, and 16.

Combine the decomposition, base rate, and adjustments into one Forecast. No tools:
everything it needs is already in state, and giving it search here would let it
re-litigate the earlier steps instead of committing to them.

On a retry it is told exactly which methodology check failed, so the second attempt
is a correction rather than a re-roll.
"""

from __future__ import annotations

from config import get_synthesis_limits, resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import (
    CheckViolation,
    Decomposition,
    Forecast,
    ForecastInput,
    InsideView,
    OutsideView,
)
from ..observability import run_agent
from . import as_of_note, format_question, with_model

INSTRUCTIONS = """You produce the final Forecast from work already done. You have no
search tools — the evidence gathering is finished. Commit to a number.

THE ARITHMETIC (principle 6 — regression to the mean)
Your probability should equal:
    aggregate_base_rate + sum of the signed, non-noise adjustments
Adjustments marked as noise contribute zero. If that sum feels wrong, the fix is to
say so in `reasoning` — not to quietly land somewhere else. A check verifies your
number against this sum and will send it back if they diverge.

This is what stops a compelling narrative pulling the estimate away from the evidence.
Extreme recent signals revert; the reference class is the gravity.

GRANULARITY (principle 8)
Use the number the arithmetic gives you. 0.63 and 0.37 are fine answers. Do not round
to a comfortable 0.60 or 0.35 for presentation — the finer gradation carries real
information. Equally, do not manufacture false precision: 0.60 is correct when that is
where the arithmetic lands.

CALIBRATION OVER BOLDNESS (principle 16)
A well-calibrated 60% beats a miscalibrated 90%. Stay inside [0.02, 0.98] unless you
are genuinely confident AND the reference classes agree — near-certainty has to be
earned by the outside view, not asserted by the narrative.

Set `confidence` from the quality of the evidence, not the strength of your opinion:
    high    multiple agreeing reference classes, solid sample sizes, clear evidence
    medium  usable base rate, some real uncertainty
    low     thin or conflicting evidence, loose reference class fit

REASONING
Trace it in order: base rate -> adjustments -> final. Name the adjustments that moved
the number most. State what would change your mind. Carry the decomposition through
into `decompositions` and the research into `research`.
"""


def build_synthesize_agent(model: str | None = None) -> Agent[ForecastDeps, Forecast]:
    return Agent[ForecastDeps, Forecast](
        model=model or resolve_agent_model(),
        name="synthesize_agent",
        deps_type=ForecastDeps,
        output_type=Forecast,
        system_prompt=INSTRUCTIONS,
        tools=[],
        retries=1,
    )


_agent: Agent[ForecastDeps, Forecast] | None = None


def get_synthesize_agent() -> Agent[ForecastDeps, Forecast]:
    global _agent
    if _agent is None:
        _agent = build_synthesize_agent()
    return _agent


def _implied(outside: OutsideView, inside: InsideView) -> float:
    """The probability the agent's own numbers point at. Mirrors checks.check_derivation."""
    total = outside.aggregate_base_rate + sum(
        (
            0.0
            if a.is_noise or a.direction == "neutral"
            else (a.magnitude if a.direction == "up" else -a.magnitude)
        )
        for a in inside.adjustments
    )
    return min(1.0, max(0.0, total))


def _violation_block(violations: list[CheckViolation]) -> str:
    if not violations:
        return ""
    lines = "\n".join(
        f"  - Principle {v.principle} ({v.name}): {v.detail}" for v in violations
    )
    return f"""

YOUR PREVIOUS ATTEMPT FAILED THESE METHODOLOGY CHECKS:
{lines}

Fix these specifically. Do not start over — the decomposition, base rate, and
adjustments below are unchanged and still stand."""


async def run_synthesize(
    input: ForecastInput,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    violations: list[CheckViolation],
    deps: ForecastDeps,
) -> Forecast:
    """Produce the final Forecast. No tools; single-shot with a small retry budget."""
    implied = _implied(outside, inside)

    prompt = f"""Produce the final Forecast.

{format_question(input)}{as_of_note(deps)}

DECOMPOSITION:
{decomposition.model_dump_json(indent=2)}

OUTSIDE VIEW:
{outside.model_dump_json(indent=2)}

INSIDE VIEW:
{inside.model_dump_json(indent=2)}

ARITHMETIC CHECK — base rate {outside.aggregate_base_rate:.3f} plus the signed
non-noise adjustments implies {implied:.3f}. Your probability should match this
unless you explain the divergence in reasoning.{_violation_block(violations)}

Fill question, resolution_criteria, resolution_date, and category from the input
exactly. Carry the sub-claims into `decompositions`."""

    agent = get_synthesize_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            usage_limits=get_synthesis_limits(),
            run_name="synthesize",
        )
    return result.output
