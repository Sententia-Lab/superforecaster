"""Update agent — principles 10, 11, and 12. States P(E|H) and P(E|not H) for each
new fact, so `checks.check_bayes_direction` can audit the move."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.capabilities.hooks import Hooks

from ..config import get_budget, get_settings
from ..deps import ForecastDeps
from ..models import ForecastRecord, UpdateDecision
from ..runner import run_agent
from ..tools import extract_pages, search_research, search_web
from . import format_history, withdraw_tools

INSTRUCTIONS = """You decide whether new evidence should move an existing probability.
You have a prior and you are revising it.

BAYESIAN UPDATING (principle 11)
For every genuinely new fact state `p_if_true` (how likely you would see it if the
outcome WILL happen) and `p_if_false`. Direction must be consistent: facts more expected
in a true world mean a higher posterior. A check verifies this. A fact with
`p_if_true == p_if_false` tells you nothing — that is the honest way to record news
that is not evidence.

FREQUENT, SMALL UPDATES (principle 10)
Most days there is nothing to move. Do not move for restated evidence, absence of news,
or a marginal move you cannot defend with a specific fact. If nothing new arrived,
return posterior == prior with an empty evidence list.

UNDER- AND OVER-REACTION (principle 12)
Under-reaction is the common error: if your facts carry weight, the number must move.
Dramatic news is often less decisive than it feels, but a bankruptcy filing or a
regulator taking control is real — then a large move is correct.

Set `posterior` with granularity: 0.62, not 0.60."""

VERIFY_INSTRUCTIONS = """You are re-examining a LARGE probability move you just made.

1. CORROBORATE: find a second, independent source for the decisive claim. One outlet
   repeating a wire story is not independent.
2. TRY TO BREAK IT: start with `search_research` (what the original forecast read), then
   search the web for evidence the claim is wrong, premature, disputed, or narrower than
   the headline. "Agreed in principle" is not "closed".

If the move survives, keep it and add the corroborating source to your evidence. If it
does not, walk it back and say what did not hold up. Do not reflexively shrink it."""

agent = Agent[ForecastDeps, UpdateDecision](
    name="update",
    deps_type=ForecastDeps,
    output_type=UpdateDecision,
    instructions=INSTRUCTIONS,
    tools=[search_research, search_web, extract_pages],
    capabilities=[Hooks(prepare_tools=withdraw_tools)],
    retries=1,
)


def _question_block(record: ForecastRecord) -> str:
    return f"""QUESTION: {record.question}

RESOLUTION CRITERIA: {record.resolution_criteria}

RESOLUTION DATE: {record.resolution_date.isoformat()}"""


def _update_prompt(record: ForecastRecord, prior: float) -> str:
    hours = get_settings().search_lookback_hours
    return f"""Decide whether this probability should move.

{_question_block(record)}

CATEGORY: {record.category}

CURRENT PROBABILITY (your prior): {prior:.3f}

UPDATE HISTORY:
{format_history(record)}

Search for developments in the last {hours} hours. Return prior={prior:.3f}; if nothing
new arrived, posterior={prior:.3f} with an empty evidence list."""


def _verify_prompt(record: ForecastRecord, from_p: float, to_p: float) -> str:
    return f"""{VERIFY_INSTRUCTIONS}

{_question_block(record)}

THE MOVE UNDER REVIEW: {from_p:.3f} -> {to_p:.3f}

UPDATE HISTORY:
{format_history(record)}

Corroborate or walk it back. Return prior={from_p:.3f}."""


async def run_update(
    record: ForecastRecord,
    deps: ForecastDeps,
    *,
    verify: tuple[float, float] | None = None,
) -> UpdateDecision:
    """Decide whether the probability should move. `verify=(prior, posterior)` switches
    the agent into the large-move corroboration pass."""
    prior = record.updates[-1].probability if record.updates else 0.5
    if verify is not None:
        prompt, run_name = _verify_prompt(record, *verify), "verify large move"
    else:
        prompt, run_name = _update_prompt(record, prior), "update"
    result = await run_agent(
        agent, prompt, deps=deps, budget=get_budget("update"), run_name=run_name
    )
    return result.output
