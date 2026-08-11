"""Run the decompose agent against a fixed set of questions and score its output.

This runs the real model, so it costs money and needs a real API key. That is why it
is a script rather than a pytest test — `uv run pytest` stubs the key and never
reaches a provider.

    make eval decompose
    make eval decompose ARGS="--model anthropic:claude-haiku-4-5 --budget 0.05,40000,0,3"

Two kinds of evaluator run on every case. `Structure`, `MentionsTerms`, and
`ChainRuleIs` are mechanical — they answer yes or no, cost nothing, and never
disagree with themselves. The `LLMJudge` asks a second model to grade the
decomposition against `RUBRIC` and return a score with a rationale, which catches the
failures no assertion can express.

Everything that runs the cases and prints the report is pydantic-evals. What this
module owns is the cases, what counts as good, and the flags.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import resolve_agent_model
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge
from pydantic_evals.evaluators.common import OutputConfig

from .. import checks
from ..agents.decompose import run_decompose
from ..deps import ForecastDeps
from ..models import Decomposition, ForecastInput
from ..observability import configure_logfire

Ctx = EvaluatorContext[ForecastInput, Decomposition, dict]

BUDGET_FORMAT = "COST,TOKENS,TOOL_CALLS,ITERATIONS"


@dataclass
class Structure(Evaluator[ForecastInput, Decomposition, dict]):
    """What every decomposition owes, whatever the question was."""

    def evaluate(self, ctx: Ctx) -> dict[str, bool]:
        out = ctx.output
        return {
            "enough_sub_questions": len(out.sub_questions) >= 3,
            "has_researchable": any(
                s.knowability == "researchable" for s in out.sub_questions
            ),
            "chain_explained": bool(out.chain_note.strip()),
            "check_clean": checks.check_decomposition(out) is None,
        }


@dataclass
class MentionsTerms(Evaluator[ForecastInput, Decomposition, dict]):
    """Did the sub-questions reach the driver this question turns on?"""

    def evaluate(self, ctx: Ctx) -> bool:
        terms = (ctx.metadata or {}).get("must_mention", [])
        text = " ".join(s.question.lower() for s in ctx.output.sub_questions)
        return all(term.lower() in text for term in terms)


@dataclass
class ChainRuleIs(Evaluator[ForecastInput, Decomposition, dict]):
    """Did it pick the combining rule the question actually has?

    Skipped (scores True) for a case that names no expected rule, because a question
    with no obvious rule should not fail the one case that has one.
    """

    def evaluate(self, ctx: Ctx) -> bool:
        expected = (ctx.metadata or {}).get("chain_rule")
        return expected is None or ctx.output.chain_rule == expected


RUBRIC = """You are grading a decomposition of a forecasting question into
sub-questions. Judge these five things, and say which ones failed.

1. TRACTABILITY. Each sub-question is narrower than the original and could be argued
   about on its own. A sub-question that restates the original question, or that is
   as hard as the original, fails.
2. COVERAGE. The sub-questions together determine the answer. If a driver that
   plainly moves the outcome is missing, that fails.
3. THE CHAIN. `chain_rule` matches how the parts actually combine — conjunction when
   every part must hold, disjunction when any one suffices — and `chain_note` says
   why in prose rather than restating the rule.
4. KNOWABILITY. `researchable` is used for sub-questions that have a real reference
   class, and `judgment` for the rest. Labelling everything one way fails. Labelling
   something researchable that has no reference class fails.
5. NOTHING SETTLED. No sub-question asks about an event the question itself already
   states has happened. If the question says the company already filed, a
   "will they file?" sub-question fails this.

Score 1.0 when all five hold. Subtract 0.2 for each one that fails. In the reason,
name the numbered items that failed and quote the sub-question at fault. If they all
hold, say so in one sentence."""


def judge(model: str) -> LLMJudge:
    """Grade the decomposition with a second model. Score plus rationale, not pass/fail.

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

    `model` rides on `ForecastDeps` rather than being passed to the agent builder,
    because that is the seam production uses: `agents.with_model` overrides the agent
    for the duration of the run. None means the agent keeps whatever
    `resolve_agent_model()` gives it.
    """

    async def task(input: ForecastInput) -> Decomposition:
        return await run_decompose(input, ForecastDeps(model=model))

    return task


CASES = [
    Case(
        name="openai_ipo",
        inputs=ForecastInput(
            question="Will OpenAI complete an initial public offering before 2028?",
            resolution_criteria=(
                "YES if OpenAI common stock trades on a public exchange before "
                "2028-01-01. A direct listing counts. A private tender offer or a "
                "SPAC announcement without completed trading does not."
            ),
            resolution_date=datetime(2027, 12, 31, tzinfo=UTC),
            category="business",
        ),
        metadata={
            # The corporate restructuring is the gate this question turns on. A prefix
            # rather than a whole word: the agent calls it "structure", "restructuring",
            # or "structural" run to run, and all three name the same driver.
            "must_mention": ["structur"],
            "chain_rule": "conjunction",
        },
    ),
    Case(
        name="taiwan_blockade",
        inputs=ForecastInput(
            question=(
                "Will China impose a naval blockade or quarantine of Taiwan lasting "
                "more than 72 hours before 2027?"
            ),
            resolution_criteria=(
                "YES if a state-directed Chinese naval or coast guard operation stops "
                "or inspects commercial shipping bound for Taiwan for more than 72 "
                "consecutive hours before 2027-01-01, per reporting by two of "
                "Reuters, AP, or the Financial Times. Live-fire exercises that do not "
                "stop commercial traffic do not count."
            ),
            resolution_date=datetime(2026, 12, 31, tzinfo=UTC),
            category="geopolitics",
        ),
        metadata={"must_mention": ["taiwan"]},
    ),
    Case(
        name="already_filed",
        inputs=ForecastInput(
            question=(
                "Will Acme Robotics list on the NYSE before 2027? Acme filed its S-1 "
                "with the SEC in March 2026 and the filing is public."
            ),
            resolution_criteria=(
                "YES if Acme Robotics shares trade on the NYSE before 2027-01-01."
            ),
            resolution_date=datetime(2026, 12, 31, tzinfo=UTC),
            category="business",
        ),
        metadata={"must_mention": ["list"]},
    ),
]


def build_dataset(judge_model: str) -> Dataset:
    return Dataset(
        name="decompose",
        cases=CASES,
        evaluators=[Structure(), MentionsTerms(), ChainRuleIs(), judge(judge_model)],
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="decompose_eval", description=__doc__)
    parser.add_argument(
        "--model",
        help="Model the decompose agent runs on. Default: the configured agent model.",
    )
    parser.add_argument(
        "--judge-model",
        help=(
            "Model that grades the output. Default: the configured agent model, "
            "which deliberately does NOT follow --model."
        ),
    )
    parser.add_argument(
        "--budget",
        metavar=BUDGET_FORMAT,
        help=(
            "Override what one decompose run may spend, in the field order of "
            "`config.Budget` — the same format as the BUDGET_DECOMPOSE env var. "
            "Example: 0.30,120000,0,6"
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
        # `config.get_budget` re-reads the environment on every call, so the env var is
        # the supported override and nothing has to be threaded through the agent.
        if len(args.budget.split(",")) != 4:
            print(f"--budget takes four fields: {BUDGET_FORMAT}", file=sys.stderr)
            return 2
        os.environ["BUDGET_DECOMPOSE"] = args.budget

    # `run_agent` configures logfire too, but only once the first case is already
    # running — and a span opened before `logfire.configure()` is never exported. The
    # experiment span opens before any of that, so configure here or lose the tree.
    configure_logfire()

    # The judge holds still while `--model` moves. It defaults to the configured agent
    # model rather than to `--model` so that scores stay comparable between runs, and
    # so a weak model under test never grades its own work — asked to, it passes itself.
    dataset = build_dataset(args.judge_model or resolve_agent_model())
    report = dataset.evaluate_sync(
        make_task(args.model),
        name="decompose",
        max_concurrency=args.concurrency,
    )
    # `include_reasons` is what names each assertion and prints the judge's rationale.
    # Rich falls back to 80 columns when stdout is not a terminal, which wraps that
    # rationale down to one word per line, so give it room when the output is piped.
    report.print(
        width=shutil.get_terminal_size((140, 24)).columns,
        include_input=False,
        include_output=False,
        include_reasons=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
