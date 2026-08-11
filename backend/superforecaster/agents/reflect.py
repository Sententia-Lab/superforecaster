"""Reflect agent — principles 14 and 15.

The whole-question half of the inside view, run after that row's barrier with every
column's adjustments in front of it and no tools.

Split out from `inside_view` because the inside view now fans out per column, and these
two principles cannot be asked of one column:

  - `bias_checks` is exactly five entries on a closed set of names. Five columns give
    twenty-five, and there is no honest merge — concatenating five `confirmation`
    assessments produces text no reader wants, picking one discards four.
  - Three of the five are questions about the *final probability*. "Would I give the
    same number for a 10x bigger version" and "am I stuck near the first number I saw"
    have no meaning inside a column, which has no number. Asking anyway produces five
    plausible paragraphs about nothing, which is the failure P15 exists to catch.
  - `check_disconfirming` fails when every adjustment points the same direction. No
    column can evaluate that; this pass sees all of them.

The INSTRUCTIONS below are lifted from `inside_view.INSTRUCTIONS` rather than rewritten,
so the wording that produced today's outputs is preserved.
"""

from __future__ import annotations

from ..config import get_budget, get_model_settings, resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import (
    Adjustment,
    Decomposition,
    ForecastInput,
    OutsideView,
    Reflection,
)
from ..runner import run_agent
from . import as_of_note, format_question, with_model

INSTRUCTIONS = """You review a forecast that has already been researched, and supply the
two things nobody looking at one piece of it could see. You do not adjust anything and
you do not produce a probability — both are already decided.

You are given every adjustment made across every part of the question, and the
sub-question-level counter-arguments the researchers wrote. You have no search tools.

SEEK DISCONFIRMATION (principle 14)
    steel_man: the strongest version of the opposing case for the WHOLE question — argue
      it properly, as someone who believes it would, not as a strawman you can dismiss.
      Draw on the per-sub-question counter-arguments you were given, but make the case
      against the forecast as a whole.
    what_would_change_my_mind: the specific observation that would move the estimate most.

If every adjustment you were given points the same direction, say so in `steel_man`.
That pattern is the strongest available evidence that the research did not look hard
enough, and it is visible only from here.

BIAS CHECK (principle 15)
Address all five, each with something actually said about THIS forecast — not
"considered and rejected":
    confirmation         did the research search for what it expected to find?
    availability         is a vivid or recent case being overweighted?
    narrative            is this believable mainly because it makes a good story?
    scope_insensitivity  would the same number come out for a 10x bigger version?
    anchoring            is the estimate stuck near the first number that appeared?

Name the specific adjustment or reference class you are pointing at wherever you can. A
bias check that would read the same on any forecast is not a bias check.
"""


def build_reflect_agent(model: str | None = None) -> Agent[ForecastDeps, Reflection]:
    return Agent[ForecastDeps, Reflection](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
        name="reflect",
        deps_type=ForecastDeps,
        output_type=Reflection,
        system_prompt=INSTRUCTIONS,
        tools=[],
        retries=1,
    )


_agent: Agent[ForecastDeps, Reflection] | None = None


def get_reflect_agent() -> Agent[ForecastDeps, Reflection]:
    global _agent
    if _agent is None:
        _agent = build_reflect_agent()
    return _agent


async def run_reflect(
    input: ForecastInput,
    decomposition: Decomposition,
    outside: OutsideView,
    adjustments: list[Adjustment],
    steel_mans: dict[str, str],
    deps: ForecastDeps,
) -> Reflection:
    """P14 + P15 over the merged inside view. No tools; one request."""
    moves = "\n".join(
        f"  - [{', '.join(a.sub_question_ids) or 'whole question'}] "
        f"{'noise (0)' if a.is_noise else f'{a.direction} {a.magnitude:.2f}'}: {a.evidence}"
        for a in adjustments
    )
    per_column = "\n".join(
        f"  - {cid}: {text}" for cid, text in steel_mans.items() if text.strip()
    )

    prompt = f"""Review this forecast's inside view.

{format_question(input)}{as_of_note(deps)}

BASE RATE THE ADJUSTMENTS MOVE FROM: {outside.aggregate_base_rate:.3f}
How the sub-questions combine: {decomposition.chain_rule} — {decomposition.chain_note}

EVERY ADJUSTMENT MADE, ACROSS EVERY PART OF THE QUESTION:
{moves or "  (none)"}

COUNTER-ARGUMENTS THE RESEARCHERS WROTE, PER SUB-QUESTION:
{per_column or "  (none)"}

Return a Reflection: one steel_man and one what_would_change_my_mind for the whole
question, plus all five bias checks."""

    agent = get_reflect_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            budget=get_budget(agent.name),
            run_name="reflect",
        )
    return result.output
