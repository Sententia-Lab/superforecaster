"""Synthesis agent — principles 6, 8, and 16.

Combine the decomposition, base rate, and adjustments into one Forecast. No tools:
everything it needs is already in state, and giving it search here would let it
re-litigate the earlier steps instead of committing to them.

On a retry it is told exactly which methodology check failed, so the second attempt
is a correction rather than a re-roll.
"""

from __future__ import annotations

from config import (
    get_model_settings,
    CheckThresholds,
    get_check_thresholds,
    get_synthesis_limits,
    resolve_agent_model,
)
from pydantic_ai import Agent

from .. import checks
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
Your probability should equal what the pipeline already computed:
    each population's measured rate, moved by its own modifiers
    blended by relevance within each sub-question
    combined by the decomposition's chain rule
Adjustments marked as noise contribute zero. The number is given to you below. If it
feels wrong, the fix is to say so in `reasoning` — not to quietly land somewhere else. A
check verifies your number against it and will send it back if they diverge.

This is what stops a compelling narrative pulling the estimate away from the evidence.
Extreme recent signals revert; the reference class is the gravity.

GRANULARITY (principle 8)
Use the number the arithmetic gives you. 0.63 and 0.37 are fine answers. Do not round
to a comfortable 0.60 or 0.35 for presentation — the finer gradation carries real
information. Equally, do not manufacture false precision: 0.60 is correct when that is
where the arithmetic lands.

CALIBRATION OVER BOLDNESS (principle 16)
A well-calibrated 60% beats a miscalibrated 90%. Near-certainty has to be earned by the
outside view, not asserted by the narrative. Your band is stated below, with the
arithmetic — it comes from configuration, so use the numbers you are given rather than
any you remember.

This is firm guidance, not a wall: go outside the band when the evidence genuinely
supports it. But then you must fill in `extreme_justification` — which reference class
carries the extreme, why the spread between the classes does not undercut it, and what
would have to be true for this to be wrong.

If you cannot write that justification, the number is telling you it is wrong. Move it,
rather than leaving the field empty. Leave `extreme_justification` empty when your
probability is inside the band.

REASONING
Trace it in order: base rate -> adjustments -> final. Name the adjustments that moved
the number most. State what would change your mind. Carry the decomposition through
into `decompositions` and the research into `research`.
"""


def build_synthesize_agent(model: str | None = None) -> Agent[ForecastDeps, Forecast]:
    return Agent[ForecastDeps, Forecast](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
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


def _implied(
    outside: OutsideView, inside: InsideView, decomposition: Decomposition | None = None
) -> float:
    """The probability the agent's own numbers point at.

    Delegates rather than mirrors. This used to be a hand-copied second implementation of
    the same arithmetic, which is exactly how the prompt and the check drifted into
    telling the agent one thing and failing it for another.
    """
    return checks.implied_probability(outside, inside, decomposition)


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


def _arithmetic_block(outside: OutsideView, implied: float) -> str:
    return (
        f"ARITHMETIC CHECK — each population moved by its own modifiers, blended by\n"
        f"relevance within each sub-question, then combined by the chain rule, implies\n"
        f"{implied:.3f}. Your probability should match this unless you explain the\n"
        f"divergence in reasoning."
    )


def _calibration_block(t: CheckThresholds | None = None) -> str:
    """The P16 band, injected at run time rather than stated in `INSTRUCTIONS`.

    The thresholds are configuration (ADR 14) and the agent is a module-level singleton,
    so a band written into the system prompt would keep claiming [0.02, 0.98] after an
    operator moved it. Same reason `_arithmetic_block` is built per run.
    """
    th = t if t is not None else get_check_thresholds()
    return (
        f"CALIBRATION BAND — [{th.calibration_floor:.2f}, {th.calibration_ceiling:.2f}].\n"
        f"Outside it, `extreme_justification` is required; inside it, leave that field empty."
    )


async def run_synthesize(
    input: ForecastInput,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    violations: list[CheckViolation],
    deps: ForecastDeps,
) -> Forecast:
    """Produce the final Forecast. No tools; single-shot with a small retry budget."""
    implied = _implied(outside, inside, decomposition)

    prompt = f"""Produce the final Forecast.

{format_question(input)}{as_of_note(deps)}

DECOMPOSITION:
{decomposition.model_dump_json(indent=2)}

OUTSIDE VIEW:
{outside.model_dump_json(indent=2)}

INSIDE VIEW:
{inside.model_dump_json(indent=2)}

{_arithmetic_block(outside, implied)}

{_calibration_block()}{_violation_block(violations)}

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
