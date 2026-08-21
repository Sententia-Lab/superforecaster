"""Run the criteria critic against a fixed set of questions and score it.

Like `decompose_eval`, this runs the real model, so it costs money and needs a real API
key. That is why it is a script rather than a pytest test.

    make eval critic
    make eval critic ARGS="--model anthropic:claude-haiku-4-5 --budget 2,3,40000"

The critic is the first agent here graded on its **path** as well as its answer. It has
one tool and a strict instruction about it: at most two searches, and only to confirm that
a source it is about to name really publishes what the criteria assume. A critique that
names the right source after three searches for the answer to the question is a worse run
than the identical critique after one, and no evaluator that reads only the output can
see the difference. `ToolTrajectoryJudge` reads the run.

Three tiers run on every case. `Verdict` and `NamedASource` are mechanical and read the
output; `MaxToolCalls` and `MaxModelRequests` are pydantic-evals' own span-based
evaluators and read the run. All four are free and never disagree with themselves.
`LLMJudge` grades the rewrite against `RUBRIC`. `ToolTrajectoryJudge` grades the tool
calls against `TOOL_RUBRIC`.

`MaxToolCalls` and `ToolTrajectoryJudge` both look at how many searches happened, and the
overlap is deliberate. The count is a fact and belongs in an assertion. Whether the count
was *right for this question* is a judgment — a question that already names the BLS needs
a search that a question saying "significant adoption" does not.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import (
    Evaluator,
    EvaluatorContext,
    LLMJudge,
    MaxModelRequests,
    MaxToolCalls,
)
from pydantic_evals.evaluators.common import OutputConfig

from superforecaster.agents.critic import run_critique
from superforecaster.config import get_budget, resolve_agent_model
from superforecaster.deps import ForecastDeps
from superforecaster.models import CriteriaCritique

from ..config import load_env
from ..observability import configure_logfire
from .eval_agents.trajectory import ToolTrajectoryJudge, record_trajectory


class CriticInput(BaseModel):
    """What the critic takes. Not a `ForecastInput` — it reviews a question before there
    is a forecast to attach one to."""

    question: str
    resolution_criteria: str
    resolution_date: datetime | None = None


Ctx = EvaluatorContext[CriticInput, CriteriaCritique, dict]

BUDGET_FORMAT = "TOOL_CALLS,REQUESTS,TOKENS"

PROMPT_MAX_CALLS = 2
"""What `critic.INSTRUCTIONS` permits: "At most TWO searches". The runtime ceiling in
`BUDGETS["critic"]` is 3, one higher, so a run that breaks its own instruction is caught
here by a failing case rather than by `UsageLimitExceeded` killing the run."""


@dataclass
class Verdict(Evaluator[CriticInput, CriteriaCritique, dict]):
    """Did it reach the labelled verdict, and did a `false` come with a fix?"""

    def evaluate(self, ctx: Ctx) -> dict[str, bool]:
        out = ctx.output
        label = (ctx.metadata or {}).get("is_resolvable")
        assertions = {"verdict_matches_label": out.is_resolvable == label}
        if label is False:
            assertions["said_what_it_changed"] = bool(out.what_changed.strip())
            assertions["suggested_a_fix"] = bool(out.suggested_criteria.strip())
        return assertions


@dataclass
class NamedASource(Evaluator[CriticInput, CriteriaCritique, dict]):
    """Every critique owes a named adjudicator, whatever else it found."""

    def evaluate(self, ctx: Ctx) -> bool:
        return bool(ctx.output.suggested_resolution_source.strip())


RUBRIC = """You are grading a review of a forecast question's resolution criteria. The
reviewer's rewrite is pasted straight over the author's own text, so judge it as
replacement wording, not as advice. Judge these four things, and say which failed.

1. ADJUDICABLE. Two people who disagree about the outcome, reading `suggested_criteria`
   on the resolution date with all the facts, would reach the same verdict. Any predicate
   that survives without a number, a unit, and a named publisher fails this.
2. A REAL SOURCE. `suggested_resolution_source` names a specific publication, dataset,
   register, or body — one someone could go and read. "Public filings" fails; "the SEC
   EDGAR full-text filing search" passes. A source that does not publish what the criteria
   need also fails.
3. MINIMAL. The rewrite keeps the author's intent and changes only what had to change. A
   rewrite that quietly narrows the question, or answers a different question, fails.
4. HONEST RATIONALE. `what_changed` names the edits and quotes the phrase it replaced.
   Empty is correct only when nothing changed. A list of complaints with no applied fix
   fails.

Score 1.0 when all four hold. Subtract 0.25 for each that fails. In the reason, name the
numbered items that failed and quote the text at fault."""

TOOL_RUBRIC = """THE AGENT'S TOOLS

  search_web(query: str) -> str
      Searches the web and returns the top results as text. The only tool this agent has.

WHAT ITS INSTRUCTIONS SAY ABOUT SEARCHING

  "At most TWO searches, and only to check that a source you are about to name exists and
  publishes what the criteria assume it does — a criterion resting on a statistic nobody
  publishes is not resolvable. That is the only thing worth searching for here. You are
  judging the wording of the question, not forecasting it: do not go looking for the
  answer, for background on the topic, or for a better source than the one you already
  found. Most questions need one search or none."

WHAT THIS MEANS FOR EACH SCORE

  tool_selection  A search that goes after the ANSWER to the forecast question is the
                  failure to watch for — will the event happen, what do forecasters think,
                  what is the current number. That is forecasting, and this agent was told
                  not to. Score it low however good the query is. A search aimed at
                  whether a named body publishes a given statistic is the permitted use.
  parameters      A good query names the publisher or the dataset the agent is about to
                  cite. "Norway EV registration statistics official" is weaker than
                  "Opplysningsradet for Veitrafikken monthly registrations by fuel type",
                  because the second confirms a specific source and the first goes fishing.
  call_count      The ceiling is two. Zero is correct when the criteria already name a
                  source the agent can recognise without checking — score zero calls 1.0
                  in that case, on all three. One is the normal answer. A second search
                  needs a reason the first did not settle. Hitting two because the first
                  query was badly worded is a parameters failure, not a count failure."""


def judge(model: str) -> LLMJudge:
    """Grade the rewrite with a second model. Score plus rationale, not pass/fail.

    `model` is always passed. The library default judge is an OpenAI model, and this
    project holds no OpenAI key.
    """
    return LLMJudge(
        rubric=RUBRIC,
        model=model,
        include_input=True,
        score=OutputConfig(evaluation_name="judge", include_reason=True),
        assertion=False,
    )


def make_task(model: str | None):
    """The task under evaluation, bound to a model.

    `record_trajectory` is the whole cost of making this agent's path gradable. It reads
    the run through a context variable, so `run_critique` is called exactly as production
    calls it.
    """

    async def task(input: CriticInput) -> CriteriaCritique:
        with record_trajectory():
            return await run_critique(
                question=input.question,
                resolution_criteria=input.resolution_criteria,
                resolution_date=input.resolution_date,
                deps=ForecastDeps(),
            )

    return task


CASES = [
    Case(
        name="vague_adoption",
        inputs=CriticInput(
            question=(
                "Will electric vehicles see significant adoption in Norway by 2027?"
            ),
            resolution_criteria=(
                "YES if EV adoption in Norway is significant by 2027-01-01."
            ),
            resolution_date=datetime(2027, 1, 1, tzinfo=UTC),
        ),
        # "Significant" has no threshold and the criteria name nobody. One search to find
        # who publishes Norwegian registration shares is the trajectory this case wants.
        metadata={"is_resolvable": False},
        evaluators=[MaxToolCalls(max_calls=PROMPT_MAX_CALLS)],
    ),
    Case(
        name="already_crisp",
        inputs=CriticInput(
            question=(
                "Will US CPI inflation, all items, 12-month change, be at or above 3.0% "
                "in the BLS release covering December 2026?"
            ),
            resolution_criteria=(
                "YES if the Bureau of Labor Statistics Consumer Price Index news release "
                "covering December 2026 reports a 12-month change in CPI-U, all items, "
                "not seasonally adjusted, of at least 3.0%. The figure in the release as "
                "first published settles it; later revisions do not."
            ),
            resolution_date=datetime(2027, 1, 31, tzinfo=UTC),
        ),
        # Named body, named series, named threshold, and revisions already handled. A
        # search here is the agent failing to recognise a source it plainly knows, so
        # this case holds a tighter ceiling than the prompt's.
        metadata={"is_resolvable": True},
        evaluators=[MaxToolCalls(max_calls=1)],
    ),
    Case(
        name="unpublished_statistic",
        inputs=CriticInput(
            question=(
                "Will more than 40% of UK software engineers use an AI coding assistant "
                "daily by the end of 2027?"
            ),
            resolution_criteria=(
                "YES if more than 40% of UK software engineers report daily use of an AI "
                "coding assistant by 2027-12-31, according to official statistics."
            ),
            resolution_date=datetime(2027, 12, 31, tzinfo=UTC),
        ),
        # "Official statistics" names nobody, and no UK statistical body publishes this.
        # Finding that out is exactly what the two searches are for.
        metadata={"is_resolvable": False},
        evaluators=[MaxToolCalls(max_calls=PROMPT_MAX_CALLS)],
    ),
]


def build_dataset(judge_model: str) -> Dataset:
    """Every case gets these; the per-case `MaxToolCalls` ceilings sit on the cases.

    `MaxModelRequests` reads the same number the runtime enforces. It is not a second
    ceiling — it is the eval reporting *which* case ran the agent in circles, which the
    `UsageLimitExceeded` path cannot, because that kills the run and returns a degraded
    critique with no trace of how close the others came.
    """
    return Dataset(
        name="critic",
        cases=CASES,
        evaluators=[
            Verdict(),
            NamedASource(),
            MaxModelRequests(max_requests=get_budget("critic").iterations),
            judge(judge_model),
            ToolTrajectoryJudge(rubric=TOOL_RUBRIC, model=judge_model),
        ],
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="critic_eval", description=__doc__)
    parser.add_argument(
        "--model",
        help="Model the critic runs on. Default: the configured agent model.",
    )
    parser.add_argument(
        "--judge-model",
        help=(
            "Model that grades the output and the trajectory. Default: the configured "
            "agent model, which deliberately does NOT follow --model."
        ),
    )
    parser.add_argument(
        "--budget",
        metavar=BUDGET_FORMAT,
        help=(
            "Override what one critic run may spend, in the field order of "
            "`config.Budget` — the same format as the BUDGET_CRITIC env var. "
            "Example: 0.10,60000,2,4"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Cases to run at once. Default: 3.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.budget:
        if len(args.budget.split(",")) != 3:
            print(f"--budget takes four fields: {BUDGET_FORMAT}", file=sys.stderr)
            return 2
        os.environ["BUDGET_CRITIC"] = args.budget

    # A span opened before `logfire.configure()` is never exported, and the experiment
    # span opens before the first case runs. Configure here or lose the tree.
    load_env()
    configure_logfire()
    if args.model:
        os.environ["AGENT_MODEL"] = args.model

    # The judge holds still while `--model` moves, so scores stay comparable between runs
    # and a weak model under test never grades its own path.
    dataset = build_dataset(args.judge_model or resolve_agent_model())
    report = dataset.evaluate_sync(
        make_task(args.model),
        name="critic",
        max_concurrency=args.concurrency,
    )
    report.print(
        width=shutil.get_terminal_size((140, 24)).columns,
        include_input=False,
        include_output=False,
        include_reasons=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
