"""Per-agent component tests — the true test of each agent.

The unit tests prove the plumbing works. Only this tier answers "how well does this
individual agent do its job", because only this tier runs the real model against
cases where the right answer is known.

**The harness ships; the data does not.** Each `components/<agent>.json` is `[]`.
A scorer encodes what "good output" means for its agent — that is the durable part,
and it is written here. The cases are researched content (a base rate that is
genuinely documented, a planted fact that is genuinely irrelevant); inventing them
would produce cases that look like tests and measure nothing. Filling them later is
data entry against scorers that already exist.

Each scorer returns named assertions rather than a bare pass/fail, so a failure
report says which property broke.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import checks
from ..agents.critic import run_critique
from ..agents.decompose import run_decompose
from ..agents.postmortem import run_postmortem
from ..agents.resolution import run_resolution_check
from ..agents.synthesize import run_synthesize
from ..agents.update import run_update
from ..deps import ForecastDeps
from ..model_garden import pick_clean_model
from ..models import ComponentCase, ComponentReport, ComponentScore

CASES_DIR = Path(__file__).resolve().parent / "components"

AGENTS = (
    "decompose",
    "outside_view",
    "inside_view",
    "synthesize",
    "critic",
    "resolution",
    "update",
    "postmortem",
)


def load_cases(agent: str, *, cases_dir: Path = CASES_DIR) -> list[ComponentCase]:
    """Read `components/<agent>.json`. Empty list when the file is empty or absent."""
    path = cases_dir / f"{agent}.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text() or "[]")
    return [ComponentCase.model_validate({"agent": agent, **c}) for c in raw]


def _score(
    case_id: str, assertions: dict[str, bool], detail: str = ""
) -> ComponentScore:
    return ComponentScore(
        case_id=case_id,
        passed=all(assertions.values()),
        assertions=assertions,
        detail=detail,
    )


# ---------- scorers ----------
#
# Signature: (agent output, case.expect) -> ComponentScore
# `expect` keys are per-agent and documented in each scorer.


def score_decompose(out: Any, expect: dict) -> ComponentScore:
    """P1 + P2. Did it break the question up, and label what can be looked up?

    expect: min_sub_questions (int), must_mention (list[str], case-insensitive
    substrings that should appear somewhere in the sub-questions).
    """
    text = " ".join(s.question.lower() for s in out.sub_questions)
    return _score(
        expect.get("id", ""),
        {
            "enough_sub_questions": len(out.sub_questions) >= expect.get("min_sub_questions", 3),
            "has_researchable": any(
                s.knowability == "researchable" for s in out.sub_questions
            ),
            "chain_explained": bool(out.chain_note.strip()),
            "mentions_expected_terms": all(
                term.lower() in text for term in expect.get("must_mention", [])
            ),
            "check_clean": checks.check_decomposition(out) is None,
        },
        detail=f"{len(out.sub_questions)} sub-questions",
    )


def score_outside_view(out: Any, expect: dict) -> ComponentScore:
    """P4 + P7. Did it find real reference classes with a defensible rate?

    expect: true_base_rate (float, the documented rate for this question),
    tolerance (float, default 0.15).
    """
    tolerance = expect.get("tolerance", 0.15)
    truth = expect.get("true_base_rate")
    assertions = {
        "two_or_more_lenses": len(out.lenses) >= 2,
        # A published block must cite; a counted one is audited by its analogs instead,
        # so "sourced" means every lens is backed one way or the other.
        "every_lens_sourced": all(
            any(e.source and e.source.source.strip() for e in l.evidence)
            or any(e.kind == "counted" for e in l.evidence)
            for l in out.lenses
        ),
        "every_lens_has_cases": all(
            sum(e.n for e in l.evidence) >= 1 for l in out.lenses
        ),
        "rates_are_derived": checks.check_base_rate_derivation(out) is None,
        "check_clean": checks.check_dragonfly(out) is None,
        "anchor_matches_weights": checks.check_aggregation(out) is None,
    }
    if truth is not None:
        assertions["rate_near_documented_truth"] = (
            abs(out.aggregate_base_rate - truth) <= tolerance
        )
    return _score(
        expect.get("id", ""),
        assertions,
        detail=f"aggregate {out.aggregate_base_rate:.3f} from {len(out.lenses)} lenses",
    )


def score_inside_view(out: Any, expect: dict) -> ComponentScore:
    """P5, P9, P14, P15. Did it find the decisive fact and discard the irrelevant one?

    expect: decisive_fact (str, substring that should appear in a real adjustment),
    irrelevant_fact (str, substring that should appear marked is_noise).
    """
    real = [a for a in out.adjustments if not a.is_noise]
    noise = [a for a in out.adjustments if a.is_noise]
    joined_real = " ".join(a.evidence.lower() for a in real)
    joined_noise = " ".join(a.evidence.lower() for a in noise)

    assertions = {
        "steel_man_present": bool(out.steel_man.strip()),
        "all_five_biases": checks.check_bias_coverage(out) is None,
        "flip_tests_present": checks.check_signal_vs_noise(out) is None,
        "sought_disconfirmation": checks.check_disconfirming(out) is None,
    }
    if decisive := expect.get("decisive_fact"):
        assertions["found_decisive_fact"] = decisive.lower() in joined_real
    if irrelevant := expect.get("irrelevant_fact"):
        assertions["discarded_irrelevant_fact"] = irrelevant.lower() in joined_noise

    return _score(
        expect.get("id", ""),
        assertions,
        detail=f"{len(real)} real adjustments, {len(noise)} marked noise",
    )


def score_synthesize(out: Any, expect: dict) -> ComponentScore:
    """P6, P8, P16. Does the final number follow from its own inputs?

    expect: expected_probability (float), tolerance (float, default 0.10).
    Every checks.py validator is applied via the case's pre-built inputs.
    """
    tolerance = expect.get("tolerance", 0.10)
    assertions = {"in_unit_interval": 0.0 <= out.probability <= 1.0}
    if (target := expect.get("expected_probability")) is not None:
        assertions["probability_near_expected"] = (
            abs(out.probability - target) <= tolerance
        )
    return _score(expect.get("id", ""), assertions, detail=f"p={out.probability:.3f}")


def score_critic(out: Any, expect: dict) -> ComponentScore:
    """P3. Precision/recall on resolvability, over deliberately good and bad criteria.

    expect: is_resolvable (bool, the label), known_ambiguity (str, a phrase the
    critic should have flagged on a bad case).
    """
    label = expect.get("is_resolvable")
    assertions = {"verdict_matches_label": out.is_resolvable == label}
    if label is False:
        assertions["named_an_ambiguity"] = bool(out.ambiguities or out.missing)
        assertions["suggested_a_fix"] = bool(out.suggested_criteria.strip())
        if phrase := expect.get("known_ambiguity"):
            found = " ".join(out.ambiguities + out.missing).lower()
            assertions["found_the_known_ambiguity"] = phrase.lower() in found
    return _score(
        expect.get("id", ""),
        assertions,
        detail=f"is_resolvable={out.is_resolvable}, {len(out.ambiguities)} ambiguities",
    )


def score_resolution(out: Any, expect: dict) -> ComponentScore:
    """Binary classification against real ground truth — the strongest scorer here.

    expect: appears_resolved (bool, the label at this as_of).

    A false positive closes a forecast permanently and is weighted accordingly: the
    case fails outright on one, whereas a false negative merely means it gets
    re-checked tomorrow.
    """
    label = expect.get("appears_resolved")
    correct = out.appears_resolved == label
    false_positive = out.appears_resolved and not label

    assertions = {"classification_correct": correct}
    if out.appears_resolved:
        assertions["cited_evidence"] = bool((out.resolution_evidence or "").strip())
    if false_positive:
        assertions["no_false_positive"] = False

    return _score(
        expect.get("id", ""),
        assertions,
        detail=(
            "FALSE POSITIVE — would have closed a live forecast"
            if false_positive
            else f"appears_resolved={out.appears_resolved}"
        ),
    )


def score_update(out: Any, expect: dict) -> ComponentScore:
    """P10, P11, P12. Did the number move the right way, consistently with its own math?

    expect: direction ("up" | "down" | "none"), the correct response to the news.
    """
    delta = out.posterior - out.prior
    moved = "up" if delta > 1e-9 else "down" if delta < -1e-9 else "none"
    return _score(
        expect.get("id", ""),
        {
            "moved_correct_direction": moved == expect.get("direction"),
            "bayes_consistent": checks.check_bayes_direction(out) is None,
            "no_under_reaction": checks.check_update_magnitude(out) is None,
            "cited_evidence_when_moving": bool(out.evidence) or moved == "none",
        },
        detail=f"{out.prior:.3f} -> {out.posterior:.3f} ({moved})",
    )


def score_postmortem(out: Any, expect: dict) -> ComponentScore:
    """P13. Does it separate process errors from outcome noise?

    expect: verdict (str, the label). The interesting cases are 70% forecasts that
    resolved "no" with sound reasoning — a scorer that rewards calling those
    "flawed" would be teaching outcome bias.
    """
    label = expect.get("verdict")
    assertions = {
        "verdict_matches_label": out.verdict == label,
        "lesson_present": bool(out.lesson.strip()),
    }
    if label == "sound_process":
        assertions["did_not_invent_process_errors"] = not out.process_errors
        assertions["attributed_to_noise"] = bool(out.outcome_noise)
    if label == "flawed_process":
        assertions["named_a_process_error"] = bool(out.process_errors)
    return _score(expect.get("id", ""), assertions, detail=f"verdict={out.verdict}")


SCORERS: dict[str, Callable[[Any, dict], ComponentScore]] = {
    "decompose": score_decompose,
    "outside_view": score_outside_view,
    "inside_view": score_inside_view,
    "synthesize": score_synthesize,
    "critic": score_critic,
    "resolution": score_resolution,
    "update": score_update,
    "postmortem": score_postmortem,
}


# ---------- running ----------


async def _score_outside_row(input, decomposition, deps):
    """One research row, run for scoring rather than for a forecast.

    Sequential on purpose. In the pipeline this row is a per-lens fan-out; here the only
    thing that matters is the merged view a scorer reads, and a plain loop is the version
    you can single-step through.
    """
    from ..agents.lenses import run_choose_lenses
    from ..agents.outside_view import cell_deps, merge_base_rates, run_research_lens
    from ..models import SubQuestionBaseRates

    cells = [s for s in decomposition.sub_questions if s.knowability == "researchable"]
    claims: list = []
    results: list[SubQuestionBaseRates] = []
    for s in cells:
        chosen = await run_choose_lenses(input, decomposition, s, deps)
        for lens in chosen.lenses:
            cdeps = cell_deps(deps, s.id or "", input.max_iterations)
            result = await run_research_lens(input, s, lens, cdeps)
            # Identity and weight come from the *chosen* lens, same as the pipeline.
            researched = result.lens.model_copy(
                update={
                    "name": lens.name,
                    "population": lens.population,
                    "why_it_fits": lens.why_it_fits,
                    "weight": lens.weight,
                    "weight_rationale": lens.weight_rationale,
                }
            )
            claims.append(s)
            results.append(
                SubQuestionBaseRates(lens=researched, disagreement=result.disagreement)
            )
            deps.sources_seen.extend(cdeps.sources_seen)
    return merge_base_rates(claims, results, decomposition)


async def _score_inside_row(input, decomposition, outside, deps):
    """The inside-view row plus its reflect pass, run for scoring. See above."""
    from ..agents.inside_view import run_adjust_lens
    from ..agents.outside_view import cell_deps
    from ..agents.reflect import run_reflect
    from ..models import InsideView

    by_id = {s.id: s for s in decomposition.sub_questions if s.id}
    adjustments = []
    steel_mans = {}
    for lens in outside.lenses:
        sub_question = next((by_id[i] for i in lens.sub_question_ids if i in by_id), None)
        if sub_question is None:
            continue
        cdeps = cell_deps(deps, sub_question.id or "", input.max_iterations)
        result = await run_adjust_lens(
            input, sub_question, lens, outside.disagreement, cdeps
        )
        adjustments.extend(
            a.model_copy(
                update={"lens_name": lens.name, "sub_question_ids": [sub_question.id]}
            )
            for a in result.adjustments
        )
        if result.steel_man:
            steel_mans[lens.name] = result.steel_man
        deps.sources_seen.extend(cdeps.sources_seen)

    reflection = await run_reflect(
        input, decomposition, outside, adjustments, steel_mans, deps
    )
    return InsideView(
        adjustments=adjustments,
        steel_man=reflection.steel_man,
        what_would_change_my_mind=reflection.what_would_change_my_mind,
        bias_checks=reflection.bias_checks,
    )


async def _dispatch(case: ComponentCase, deps: ForecastDeps) -> Any:
    """Call the agent this case targets, with its inputs reconstructed from JSON."""
    from ..models import (
        Decomposition,
        ForecastInput,
        ForecastRecord,
        InsideView,
        OutsideView,
    )

    data = case.input
    if case.agent == "decompose":
        return await run_decompose(ForecastInput.model_validate(data["input"]), deps)
    if case.agent == "outside_view":
        return await _score_outside_row(
            ForecastInput.model_validate(data["input"]),
            Decomposition.model_validate(data["decomposition"]),
            deps,
        )
    if case.agent == "inside_view":
        # An `inside_view` case needs a decomposition as well as an outside view: the
        # row fans out per sub-question, and each cell adjusts from its own column's
        # rate rather than the whole-question anchor.
        return await _score_inside_row(
            ForecastInput.model_validate(data["input"]),
            Decomposition.model_validate(data["decomposition"]),
            OutsideView.model_validate(data["outside"]),
            deps,
        )
    if case.agent == "synthesize":
        return await run_synthesize(
            ForecastInput.model_validate(data["input"]),
            Decomposition.model_validate(data["decomposition"]),
            OutsideView.model_validate(data["outside"]),
            InsideView.model_validate(data["inside"]),
            [],
            deps,
        )
    if case.agent == "critic":
        raw_date = data.get("resolution_date")
        return await run_critique(
            question=data["question"],
            resolution_criteria=data["resolution_criteria"],
            resolution_date=datetime.fromisoformat(raw_date) if raw_date else None,
            deps=deps,
        )
    if case.agent == "resolution":
        return await run_resolution_check(
            ForecastRecord.model_validate(data["record"]), deps
        )
    if case.agent == "update":
        return await run_update(ForecastRecord.model_validate(data["record"]), deps)
    if case.agent == "postmortem":
        return await run_postmortem(ForecastRecord.model_validate(data["record"]), deps)
    raise ValueError(f"unknown agent: {case.agent}")


async def run_case(case: ComponentCase, *, mode: str = "clean") -> ComponentScore:
    """Run one case and score it.

    In clean mode a case carrying `as_of` needs a model trained before that date;
    without one the case is skipped rather than scored against a contaminated model.
    """
    model = None
    if mode == "clean" and case.as_of is not None:
        entry = pick_clean_model(case.as_of)
        if entry is None:
            return ComponentScore(
                case_id=case.id,
                skipped=(
                    f"no available model with a training cutoff before "
                    f"{case.as_of.date().isoformat()}"
                ),
            )
        model = entry.id

    deps = ForecastDeps(as_of=case.as_of, model=model)
    try:
        output = await _dispatch(case, deps)
    except Exception as exc:  # noqa: BLE001 — one bad case must not abort the run
        return ComponentScore(case_id=case.id, error=f"{type(exc).__name__}: {exc}")

    return SCORERS[case.agent](output, {**case.expect, "id": case.id})


async def run_component(
    agent: str, *, mode: str = "clean", cases_dir: Path = CASES_DIR
) -> ComponentReport:
    """Run every case for one agent."""
    if agent not in SCORERS:
        raise ValueError(
            f"unknown agent {agent!r}; expected one of {', '.join(AGENTS)}"
        )

    cases = load_cases(agent, cases_dir=cases_dir)
    scores = [await run_case(c, mode=mode) for c in cases]
    return _build_report(agent, scores)


def _build_report(agent: str, scores: list[ComponentScore]) -> ComponentReport:
    scored = [s for s in scores if s.skipped is None and s.error is None]
    rates: dict[str, float] = {}
    names = {name for s in scored for name in s.assertions}
    for name in sorted(names):
        applicable = [s for s in scored if name in s.assertions]
        rates[name] = sum(s.assertions[name] for s in applicable) / len(applicable)

    return ComponentReport(
        agent=agent,
        n=len(scores),
        pass_rate=(sum(s.passed for s in scored) / len(scored)) if scored else 0.0,
        assertion_pass_rates=rates,
        scores=scores,
    )


def render_report(report: ComponentReport) -> str:
    """Plain-text summary for the terminal."""
    if report.n == 0:
        return (
            f"{report.agent}: 0 cases — add data to "
            f"evals/components/{report.agent}.json"
        )

    skipped = [s for s in report.scores if s.skipped]
    errored = [s for s in report.scores if s.error]
    lines = [
        f"{report.agent}: {report.n} cases   "
        f"pass rate {report.pass_rate:.0%}   "
        f"{len(skipped)} skipped   {len(errored)} errored"
    ]
    for name, rate in report.assertion_pass_rates.items():
        lines.append(f"    {name.ljust(32)} {rate:.0%}")
    for s in report.scores:
        if s.skipped:
            lines.append(f"    SKIP {s.case_id}: {s.skipped}")
        elif s.error:
            lines.append(f"    ERR  {s.case_id}: {s.error}")
        elif not s.passed:
            failed = ", ".join(k for k, v in s.assertions.items() if not v)
            lines.append(f"    FAIL {s.case_id}: {failed} ({s.detail})")
    return "\n".join(lines)
