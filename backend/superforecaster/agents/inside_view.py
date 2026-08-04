"""Inside-view agent — principles 5, 9, 14, and 15.

Adjust away from the base rate using case-specific evidence. Every adjustment is a
signed delta from the outside view, never a fresh absolute estimate — that is what
makes principle 5 ("the inside view modifies the outside view") checkable arithmetic
rather than a hope.
"""

from __future__ import annotations

from config import get_research_limits, resolve_agent_model
from pydantic_ai import Agent

from ..deps import ForecastDeps
from ..models import Decomposition, ForecastInput, InsideView, OutsideView
from ..observability import run_agent
from ..tools import find_disconfirming_evidence, search_web, search_wikipedia
from . import as_of_note, format_question, with_model

INSTRUCTIONS = """You supply the INSIDE VIEW: what makes this specific case differ
from its reference class. You do not produce a final probability — a later step does.

ADJUST, DO NOT REPLACE (principle 5)
You are given a base rate. Every adjustment you return is a signed move away from it,
in probability points:
    direction  up / down / neutral
    magnitude  how many points, 0 to 0.5
Never restate an absolute probability. If the base rate is 0.20 and you think this
case is somewhat more likely, that is one adjustment up by 0.08 — not "I think 0.28".
The next step adds your deltas to the base rate, and a check verifies the final number
matches. Adjustments that do not sum to your intended answer will be caught.

THE FLIP TEST (principle 9)
For every adjustment, fill in `flip_test`: what would my estimate do if I had found
the OPPOSITE of this evidence? If the honest answer is "nothing much", the evidence is
noise dressed as signal — set `is_noise=true` and `magnitude=0`. Keep it in the list;
recording what you considered and discarded is useful. Evidence you call noise must
move the number by zero.

SEEK DISCONFIRMATION FIRST (principle 14)
Use find_disconfirming_evidence before you settle, not after. Ordinary search returns
material that agrees with however you phrased the query.
    steel_man: the strongest version of the opposing case — argue it properly, as
      someone who believes it would, not as a strawman you can dismiss.
    what_would_change_my_mind: the specific observation that would move you most.
If every adjustment points the same direction, you have not looked hard enough.

BIAS CHECK (principle 15)
Address all five, each with something actually said — not "considered and rejected":
    confirmation         did I search for what I expected to find?
    availability         am I overweighting a vivid or recent case?
    narrative            am I believing this because it makes a good story?
    scope_insensitivity  would I give the same number for a 10x bigger version?
    anchoring            am I stuck near the first number I saw?

GRADE YOUR SOURCES
For each adjustment, list what backs it in `sources`. Grade each one for how strongly
it supports *this specific* adjustment, not how reputable it is in general:
    high    directly on point, and from something that would know
    medium  relevant but indirect, dated, or partial
    low     suggestive only — a single report, an analogy, a claim by an interested party
Say why in `note`. Set `source` to a human label — the publication, dataset, or filing
("PitchBook M&A Report 2024"), never a bare URL. Put the link in `url`, and only when
you actually retrieved that page: a check verifies cited URLs against what your searches
returned, and inventing one is a violation. Copy the link exactly as the search results
gave it, in full — a partial or redirect fragment is dropped and the citation loses its
link.

Leave `sources` empty when an adjustment is a judgment call with nothing to look up.
That is an honest answer and grades as weak support. Padding the list with weak
citations does not help you — the grade for a claim is its *strongest* source, so an
extra thin one neither raises nor lowers it.

BUDGET
Limited search budget. Prefer a few well-chosen searches over exhaustive looping.
"""


def build_inside_view_agent(
    model: str | None = None,
) -> Agent[ForecastDeps, InsideView]:
    return Agent[ForecastDeps, InsideView](
        model=model or resolve_agent_model(),
        name="inside_view_agent",
        deps_type=ForecastDeps,
        output_type=InsideView,
        system_prompt=INSTRUCTIONS,
        tools=[search_web, search_wikipedia, find_disconfirming_evidence],
        retries=1,
    )


_agent: Agent[ForecastDeps, InsideView] | None = None


def get_inside_view_agent() -> Agent[ForecastDeps, InsideView]:
    global _agent
    if _agent is None:
        _agent = build_inside_view_agent()
    return _agent


async def run_inside_view(
    input: ForecastInput,
    decomposition: Decomposition,
    outside: OutsideView,
    deps: ForecastDeps,
) -> InsideView:
    """Produce signed adjustments away from the base rate. Searches; budget-limited."""
    classes = "\n".join(
        f"  - {rc.name}: {rc.base_rate:.3f} (n={rc.sample_size}, weight={rc.weight:.2f}, "
        f"{'; '.join(f'{s.source} [{s.confidence}]' for s in rc.sources)})"
        for rc in outside.reference_classes
    )
    disagreement = (
        f"\nThe reference classes disagree. Noted reason: {outside.disagreement}"
        if outside.disagreement.strip()
        else "\nThe reference classes broadly agree."
    )
    sub_claims = "\n".join(
        f"  - {s.id}: {s.question} [{s.knowability}]" for s in decomposition.sub_claims
    )

    prompt = f"""Adjust from the base rate using case-specific evidence.

{format_question(input)}{as_of_note(deps)}

BASE RATE TO ADJUST FROM: {outside.aggregate_base_rate:.3f}

REFERENCE CLASSES:
{classes}{disagreement}

SUB-CLAIMS FROM THE DECOMPOSITION:
{sub_claims}

Set `sub_claim_ids` on each adjustment to the ids above it bears on, so a reader can
see which part of the question your evidence moves. Leave it empty for an adjustment
that applies to the question as a whole.

SEARCH BUDGET: at most {input.max_iterations} rounds. Stop when it is used.

Return an InsideView. Every adjustment is a signed delta from
{outside.aggregate_base_rate:.3f}, with a flip test. All five bias checks required."""

    agent = get_inside_view_agent()
    with with_model(agent, deps) as bound:
        result = await run_agent(
            bound,
            prompt,
            deps=deps,
            verbose=deps.verbose,
            usage_limits=get_research_limits(input.max_iterations),
            run_name="inside view",
        )
    return result.output
