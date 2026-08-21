"""Run the decompose agent against a fixed set of questions and score its output.

This runs the real model, so it costs money and needs a real API key. That is why it
is a script rather than a pytest test — `uv run pytest` stubs the key and never
reaches a provider.

    make eval decompose
    make eval decompose ARGS="--model anthropic:claude-haiku-4-5 --budget 0,3,40000"

Two kinds of evaluator run on every case. `Structure`, `MentionsTerms`, and
`ChainRuleIs` are mechanical — they answer yes or no, cost nothing, and never
disagree with themselves. The `LLMJudge` asks a second model to grade the
decomposition against `RUBRIC` and return a score with a rationale, which catches the
failures no assertion can express.

Everything that runs the cases and prints the report is pydantic-evals. What this
module owns is the cases, what counts as good, and the flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge
from pydantic_evals.evaluators.common import OutputConfig

from . import eval_main
from superforecaster import checks
from superforecaster.agents.decompose import run_decompose
from superforecaster.deps import ForecastDeps
from superforecaster.models import Decomposition, ForecastInput

Ctx = EvaluatorContext[ForecastInput, Decomposition, dict]


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
6. 

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


def make_task():
    """The task under evaluation. `--model` is applied through `AGENT_MODEL`."""

    async def task(input: ForecastInput) -> Decomposition:
        return await run_decompose(input, ForecastDeps())

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
        evaluators=[
            Structure(),
            MentionsTerms(),
            ChainRuleIs(),
            judge(judge_model),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    return eval_main("decompose", build_dataset, make_task, argv)


if __name__ == "__main__":
    raise SystemExit(main())
