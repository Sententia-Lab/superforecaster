"""Reflect agent — principles 14 and 15, over the whole question. Runs after every
inside-view cell with all adjustments in front of it (ADR 31)."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks

from ..config import get_budget
from ..deps import ForecastDeps
from ..models import Adjustment, Decomposition, ForecastInput, OutsideView, Reflection
from ..runner import run_agent
from ..tools import search_research
from . import format_question, withdraw_tools

INSTRUCTIONS = """You review a researched forecast and supply what no single part of it
could see. You do not adjust anything and you do not produce a probability.

SEEK DISCONFIRMATION (principle 14)
  steel_man: the strongest case against the forecast as a whole, argued as a believer
    would. Draw on the per-part counter-arguments you are given. If every adjustment
    points the same direction, say so — that is evidence the research did not look
    hard enough.
  what_would_change_my_mind: the specific observation that would move the estimate most.

BIAS CHECK (principle 15)
Address all five with something specific to THIS forecast — name the adjustment or
reference class you mean:
    confirmation         did the research search for what it expected to find?
    availability         is a vivid or recent case being overweighted?
    narrative            is this believable mainly because it makes a good story?
    scope_insensitivity  would the same number come out for a 10x bigger version?
    anchoring            is the estimate stuck near the first number that appeared?"""

agent = Agent[ForecastDeps, Reflection](
    name="reflect",
    deps_type=ForecastDeps,
    output_type=Reflection,
    instructions=INSTRUCTIONS,
    tools=[search_research],
    capabilities=[Hooks(prepare_tools=withdraw_tools)],
    retries=1,
)


async def run_reflect(
    input: ForecastInput,
    decomposition: Decomposition,
    outside: OutsideView,
    adjustments: list[Adjustment],
    steel_mans: dict[str, str],
    deps: ForecastDeps,
) -> Reflection:
    moves = "\n".join(
        f"  - [{', '.join(a.sub_question_ids) or 'whole question'}] "
        f"{'noise (0)' if a.is_noise else f'{a.direction} {a.magnitude:.2f}'}: {a.evidence}"
        for a in adjustments
    )
    per_part = "\n".join(
        f"  - {name}: {text}" for name, text in steel_mans.items() if text.strip()
    )
    prompt = f"""Review this forecast's inside view.

{format_question(input)}

BASE RATE THE ADJUSTMENTS MOVE FROM: {outside.aggregate_base_rate:.3f}
How the sub-questions combine: {decomposition.chain_rule} — {decomposition.chain_note}

EVERY ADJUSTMENT MADE, ACROSS EVERY PART OF THE QUESTION:
{moves or "  (none)"}

COUNTER-ARGUMENTS THE RESEARCHERS WROTE, PER LENS:
{per_part or "  (none)"}"""
    result = await run_agent(
        agent, prompt, deps=deps, budget=get_budget("reflect"), run_name="reflect"
    )
    return result.output
