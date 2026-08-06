"""Post-mortem agent — principle 13.

Standalone: runs after a forecast resolves, not inside either graph.

Its whole job is one distinction. A 70% forecast that resolved "no" is not
automatically a mistake — 30% of the time that is exactly what should happen. Judging
a forecast by its outcome is outcome bias, and a system that learns from outcome bias
learns to be overconfident.

So: separate what the reasoning got wrong (fixable) from what was genuinely unknowable
at the time (noise). Only the first kind should change how the agent forecasts.
"""

from __future__ import annotations

from config import get_monitor_limits, resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import ForecastRecord, PostMortem
from ..observability import run_agent
from ..tools import search_web
from . import with_model

INSTRUCTIONS = """You review resolved forecasts to find process errors. You are not
grading the outcome.

THE CENTRAL DISTINCTION
A 70% forecast that resolved "no" may have been excellent. A 90% forecast that
resolved "yes" may have been reckless. The question is never "was it right" — it is
"given only what was knowable at the time, was this good reasoning?"

  process_errors   things the forecast got wrong that were knowable then:
                     - a reference class that did not fit, or a better one ignored
                     - a base rate abandoned for a narrative
                     - available evidence not sought
                     - an adjustment far larger than its evidence supported
                     - a stated update that contradicted its own reasoning
                     - overconfidence: a probability the evidence did not support

  outcome_noise    things that genuinely could not have been known:
                     - a low-probability event that happened to occur
                     - information that did not exist at forecast time
                     - a resolution turning on an ambiguity in the criteria

VERDICT
  sound_process           reasoning was good; the outcome does not change that
  flawed_process          identifiable errors that were avoidable at the time
  insufficient_evidence   the reasoning trace is too thin to judge either way

Do not slide toward "flawed" because the forecast missed. A miss with sound reasoning
is `sound_process` — say so plainly. Equally, do not excuse a lucky hit: a forecast
that landed on the right side for the wrong reasons is `flawed_process`.

LESSON
One transferable sentence that would improve the NEXT forecast. Not "should have been
more careful" — something specific enough to act on. If the process was sound, the
honest lesson may be "nothing to change here", and that is a real finding.

You may search to establish what was publicly knowable before the forecast date.
"""


def build_postmortem_agent(model: str | None = None) -> Agent[ForecastDeps, PostMortem]:
    return Agent[ForecastDeps, PostMortem](
        model=model or resolve_agent_model(),
        name="postmortem_agent",
        deps_type=ForecastDeps,
        output_type=PostMortem,
        system_prompt=INSTRUCTIONS,
        tools=[search_web],
        retries=1,
    )


_agent: Agent[ForecastDeps, PostMortem] | None = None


def get_postmortem_agent() -> Agent[ForecastDeps, PostMortem]:
    global _agent
    if _agent is None:
        _agent = build_postmortem_agent()
    return _agent


def _format_updates(record: ForecastRecord) -> str:
    lines = []
    for i, u in enumerate(record.updates, 1):
        late = " [LATE]" if u.is_late else ""
        lines.append(
            f"{i}. {u.created_at.isoformat()} — p={u.probability:.3f}"
            f"{late}\n   {u.reasoning}"
        )
    return "\n".join(lines) if lines else "(no updates)"


async def run_postmortem(
    record: ForecastRecord, deps: ForecastDeps | None = None
) -> PostMortem:
    """Separate process errors from outcome noise on a resolved forecast."""
    deps = deps or ForecastDeps()
    outcome = (
        "AMBIGUOUS — excluded from scoring"
        if record.is_ambiguous
        else (
            f"{record.outcome:.1f}"
            if record.outcome is not None
            else "NOT YET RESOLVED"
        )
    )
    brier = f"{record.brier_score:.4f}" if record.brier_score is not None else "n/a"
    scored = (
        f"{record.scored_probability:.3f}"
        if record.scored_probability is not None
        else "n/a"
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
{[s.model_dump() for s in record.decompositions]}

RESEARCH:
{record.research.model_dump_json(indent=2)}

PROBABILITY HISTORY:
{_format_updates(record)}

Judge the reasoning as of {record.created_at.date().isoformat()}, not with hindsight.
Return a PostMortem."""

    agent = get_postmortem_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            usage_limits=get_monitor_limits(),
            run_name="post-mortem",
        )
    return result.output
