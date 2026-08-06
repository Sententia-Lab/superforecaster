"""Update agent — principles 10, 11, and 12.

Decides whether new evidence should move an existing probability, and by how much.
Its distinguishing feature is that it must state P(E|H) and P(E|not H) for each new
fact, which turns "I updated because of the news" into arithmetic a pure function can
check (`checks.check_bayes_direction`).

Also runs the deep-verification pass when a probability jump is large enough that the
graph routes back through `VerifyLargeMove`.
"""

from __future__ import annotations

from config import get_model_settings, get_monitor_limits, get_settings, resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import ForecastRecord, UpdateDecision
from ..observability import run_agent
from ..tools import find_disconfirming_evidence, search_web
from . import with_model

INSTRUCTIONS = """You are deciding whether new evidence should move an existing
probability forecast. You are not producing a fresh forecast — you have a prior and
you are revising it.

BAYESIAN-FLAVOURED UPDATING (principle 11)
For every genuinely new fact, state two numbers:
    p_if_true   how likely I would be to see this fact if the outcome WILL happen
    p_if_false  how likely I would be to see this fact if it will NOT happen

Asking the question is where most of the value is; you do not have to compute an exact
posterior. But the direction must be consistent: if your facts are collectively more
expected in a true world, your posterior must be higher than your prior. A check
verifies exactly this and will flag a contradiction.

A fact with p_if_true == p_if_false tells you nothing. That is the correct way to
record something that felt like news but is not evidence.

FREQUENT, SMALL UPDATES (principle 10)
Belief revision is incremental. Most days there is nothing to move.
Do NOT move the probability for:
  - restating prior evidence in different words
  - absence of news (no news is not evidence)
  - a marginal move you cannot defend with a specific fact
If nothing new arrived, return posterior == prior with an empty evidence list. That is
a good answer, not a failure.

UNDER- AND OVER-REACTION (principle 12)
Under-reaction is the more common error: real evidence arrives and the prior holds you
in place. If your facts carry weight, the number must move.
Over-reaction is real too. Dramatic news feels more decisive on the day than it turns
out to be. But genuinely decisive events do happen — a bankruptcy filing, a regulator
taking control — and when one has, a large move is correct.

Set `posterior` to your revised probability with granularity: 0.62, not 0.60.
"""

VERIFY_INSTRUCTIONS = """You are re-examining a LARGE probability move you just made.

The move was big enough that it needs corroboration before it is written. Do two
things, in this order:

1. CORROBORATE. Find a second, independent source for the decisive claim. A single
   outlet repeating a wire story is not independent confirmation.
2. TRY TO BREAK IT. Search for evidence the claim is wrong, premature, disputed, or
   more limited than the headline implies. Reports get retracted; "agreed in
   principle" is not "closed"; an announcement is not an event.

Then return a revised UpdateDecision.
  - If the move survives, keep it. Decisive events are real and a large move is the
    correct response to one. Add the corroborating source to your evidence.
  - If it does not survive, walk it back and say what did not hold up.

Do not reflexively shrink the move. The point of this pass is to find out whether the
first read was right, not to average it toward the prior.
"""


def build_update_agent(model: str | None = None) -> Agent[ForecastDeps, UpdateDecision]:
    return Agent[ForecastDeps, UpdateDecision](
        model=model or resolve_agent_model(),
        model_settings=get_model_settings(),
        name="update_agent",
        deps_type=ForecastDeps,
        output_type=UpdateDecision,
        system_prompt=INSTRUCTIONS,
        tools=[search_web, find_disconfirming_evidence],
        retries=1,
    )


_agent: Agent[ForecastDeps, UpdateDecision] | None = None


def get_update_agent() -> Agent[ForecastDeps, UpdateDecision]:
    global _agent
    if _agent is None:
        _agent = build_update_agent()
    return _agent


def _format_history(record: ForecastRecord) -> str:
    lines = []
    for i, u in enumerate(record.updates, 1):
        late = " [LATE]" if u.is_late else ""
        lines.append(
            f"{i}. {u.created_at.isoformat()} — p={u.probability:.3f}"
            f"{late}\n   {u.reasoning}"
        )
    return "\n".join(lines) if lines else "(no updates yet)"


async def run_update(
    record: ForecastRecord,
    deps: ForecastDeps,
    *,
    verify: tuple[float, float] | None = None,
) -> UpdateDecision:
    """Decide whether the probability should move.

    `verify` carries `(prior, posterior)` when called from the VerifyLargeMove node,
    switching the agent into deep-verification mode.
    """
    lookback_hours = get_settings().search_lookback_hours
    prior = record.updates[-1].probability if record.updates else 0.5

    if verify is not None:
        from_p, to_p = verify
        prompt = f"""{VERIFY_INSTRUCTIONS}

QUESTION: {record.question}

RESOLUTION CRITERIA: {record.resolution_criteria}

RESOLUTION DATE: {record.resolution_date.isoformat()}

THE MOVE UNDER REVIEW: {from_p:.3f} -> {to_p:.3f}

UPDATE HISTORY:
{_format_history(record)}

Corroborate or walk it back. Return a revised UpdateDecision with prior={from_p:.3f}."""
        run_name = "verify large move"
    else:
        prompt = f"""Decide whether this probability should move.

QUESTION: {record.question}

RESOLUTION CRITERIA: {record.resolution_criteria}

RESOLUTION DATE: {record.resolution_date.isoformat()}

CATEGORY: {record.category}

CURRENT PROBABILITY (your prior): {prior:.3f}

UPDATE HISTORY:
{_format_history(record)}

LOOKBACK: search for developments in the last {lookback_hours} hours.

Return an UpdateDecision with prior={prior:.3f}. If nothing new arrived, set
posterior={prior:.3f} with an empty evidence list."""
        run_name = "update"

    agent = get_update_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            usage_limits=get_monitor_limits(),
            run_name=run_name,
        )
    return result.output
