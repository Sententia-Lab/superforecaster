"""Post-mortem agent — principle 13. Separates process errors (fixable) from outcome
noise (unknowable at the time), so a miss with sound reasoning is not "fixed"."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks

from ..config import get_budget
from ..deps import ForecastDeps
from ..models import ForecastRecord, PostMortem
from ..runner import run_agent
from ..tools import extract_pages, search_research, search_web
from . import format_history, withdraw_tools

INSTRUCTIONS = """You review resolved forecasts for process errors. You are not grading
the outcome: a 70% forecast that resolved "no" may have been excellent, and a 90% that
resolved "yes" may have been reckless. The question is "given only what was knowable
then, was this good reasoning?"

  process_errors   knowable then: a reference class that did not fit, a base rate
                   abandoned for a narrative, available evidence not sought, an
                   adjustment far larger than its evidence, overconfidence.
  outcome_noise    not knowable: a low-probability event that occurred, information
                   that did not exist yet, a resolution turning on ambiguous criteria.

VERDICT: `sound_process` (reasoning was good whatever the outcome), `flawed_process`
(avoidable errors), or `insufficient_evidence` (trace too thin to judge). Do not slide
toward "flawed" because the forecast missed, and do not excuse a lucky hit.

LESSON: one transferable sentence specific enough to act on. "Nothing to change" is a
real finding.

`search_research` holds the pages this forecast was built on; compare what the process
saw against what was there before searching the web for what else was knowable."""

agent = Agent[ForecastDeps, PostMortem](
    name="postmortem",
    deps_type=ForecastDeps,
    output_type=PostMortem,
    instructions=INSTRUCTIONS,
    tools=[search_research, search_web, extract_pages],
    capabilities=[Hooks(prepare_tools=withdraw_tools)],
    retries=1,
)


async def run_postmortem(
    record: ForecastRecord, deps: ForecastDeps | None = None
) -> PostMortem:
    if record.is_ambiguous:
        outcome = "AMBIGUOUS — excluded from scoring"
    elif record.outcome is not None:
        outcome = f"{record.outcome:.1f}"
    else:
        outcome = "NOT YET RESOLVED"
    brier = f"{record.brier_score:.4f}" if record.brier_score is not None else "n/a"
    scored = (
        f"{record.scored_probability:.3f}"
        if record.scored_probability is not None
        else "n/a"
    )
    decomposition = "\n".join(
        f"  - {s.question} (p={s.probability:.2f}): {s.rationale}"
        for s in record.decompositions
    )
    prompt = f"""Review this resolved forecast for process errors.

QUESTION: {record.question}

RESOLUTION CRITERIA: {record.resolution_criteria}

FORECAST DATE: {record.created_at.isoformat()}
RESOLUTION DATE: {record.resolution_date.isoformat()}

ACTUAL OUTCOME: {outcome}
TIME-WEIGHTED PROBABILITY: {scored}
BRIER SCORE: {brier}

INITIAL REASONING:
{record.initial_reasoning}

DECOMPOSITION:
{decomposition}

PROBABILITY HISTORY:
{format_history(record)}

Judge the reasoning as of {record.created_at.date().isoformat()}, not with hindsight."""
    result = await run_agent(
        agent,
        prompt,
        deps=deps or ForecastDeps(),
        budget=get_budget("postmortem"),
        run_name="post-mortem",
    )
    return result.output
