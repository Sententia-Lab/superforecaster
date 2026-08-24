"""Synthesis agent — principles 6, 8, and 16. Commits to a number the pipeline already
computed; on a retry it is told which check failed."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks

from .. import checks
from ..config import get_budget, get_check_thresholds
from ..deps import ForecastDeps
from ..models import (
    CheckViolation,
    Decomposition,
    ForecastAnswer,
    ForecastInput,
    InsideView,
    OutsideView,
)
from ..runner import run_agent
from ..tools import search_research
from . import format_question, withdraw_tools

INSTRUCTIONS = """You write the final forecast from work already done. The evidence
gathering is finished — commit to a number.

Your probability should equal the arithmetic given to you: each population's measured
rate moved by its own modifiers, blended within each sub-question, combined by the chain
rule (principle 6). If it feels wrong, say so in `reasoning` rather than landing
elsewhere; a check sends back a number that diverges unexplained. Use the number the
arithmetic gives — 0.63 is a fine answer; do not round it (principle 8).

A well-calibrated 60% beats a miscalibrated 90% (principle 16). You may go outside the
calibration band when the evidence earns it, but then `extreme_justification` must say
which reference class carries the extreme, why the spread between classes does not
undercut it, and what would have to be true for it to be wrong. Inside the band, leave
it empty.

`reasoning`: base rate -> the adjustments that moved the number most -> final, and what
would change your mind. `search_research` holds the pages this run read; use it to
check a figure, not to find new evidence."""

agent = Agent[ForecastDeps, ForecastAnswer](
    name="synthesize",
    deps_type=ForecastDeps,
    output_type=ForecastAnswer,
    instructions=INSTRUCTIONS,
    tools=[search_research],
    capabilities=[Hooks(prepare_tools=withdraw_tools)],
    retries=1,
)


def render_views(
    decomposition: Decomposition, outside: OutsideView, inside: InsideView
) -> str:
    """The three views as short lines rather than JSON dumps (ADR 81)."""
    rows = checks.chain_inputs(decomposition, outside)
    subs = "\n".join(
        f"  {r['id']} {r['rate']:.2f} ({r['source']})  {r['question']}" for r in rows
    )
    lenses = "\n".join(
        f"  {', '.join(l.sub_question_ids) or '-'} · {l.name}  w={l.weight:.2f}  "
        f"rate={checks.lens_rate(l):.2f}  ({'; '.join(f'{e.hits}/{e.n} {e.kind}' for e in l.evidence)})"
        for l in outside.lenses
    )
    moves = "\n".join(
        f"  {', '.join(a.sub_question_ids) or '-'} · {a.lens_name or '-'}  "
        f"{'noise' if a.is_noise else f'{a.direction} {a.magnitude:.2f}'}  "
        f"{a.title or a.evidence[:80]}"
        for a in inside.adjustments
    )
    biases = "\n".join(f"  {b.bias}: {b.assessment}" for b in inside.bias_checks)
    return f"""SUB-QUESTIONS (chain: {decomposition.chain_rule}) — {decomposition.chain_note}
{subs}

LENSES
{lenses}
Disagreement: {outside.disagreement or "(none)"}

ADJUSTMENTS
{moves}

STEEL MAN: {inside.steel_man}
WOULD CHANGE MY MIND: {inside.what_would_change_my_mind}
BIAS CHECKS
{biases}"""


def _violation_block(violations: list[CheckViolation]) -> str:
    if not violations:
        return ""
    lines = "\n".join(f"  - P{v.principle} ({v.name}): {v.detail}" for v in violations)
    return f"\n\nYOUR PREVIOUS ATTEMPT FAILED THESE CHECKS:\n{lines}\nFix these; everything above is unchanged."


async def run_synthesize(
    input: ForecastInput,
    decomposition: Decomposition,
    outside: OutsideView,
    inside: InsideView,
    violations: list[CheckViolation],
    deps: ForecastDeps,
) -> ForecastAnswer:
    th = get_check_thresholds()
    implied = checks.implied_probability(outside, inside, decomposition)
    prompt = f"""Produce the final forecast.

{format_question(input)}

{render_views(decomposition, outside, inside)}

ARITHMETIC: the lenses moved by their modifiers, blended, and combined by the chain
rule imply {implied:.3f}. Match it unless you explain the divergence in `reasoning`.
CALIBRATION BAND: [{th.calibration_floor:.2f}, {th.calibration_ceiling:.2f}]. Outside
it, `extreme_justification` is required.{_violation_block(violations)}"""
    result = await run_agent(
        agent, prompt, deps=deps, budget=get_budget("synthesize"), run_name="synthesize"
    )
    return result.output
