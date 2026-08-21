"""Agent evals. Each `<agent>_eval.py` runs the real model; `eval_main` is the shared
command line: `--model`, `--judge-model`, `--budget tool_calls,requests,tokens`."""

from __future__ import annotations

import argparse
import os
import shutil
from collections.abc import Callable

from superforecaster.config import resolve_agent_model

from ..config import load_env
from ..observability import configure_logfire


def eval_main(
    name: str, build_dataset: Callable, make_task: Callable, argv: list[str] | None
) -> int:
    parser = argparse.ArgumentParser(prog=f"{name}_eval")
    parser.add_argument("--model", help="Model the agent runs on")
    parser.add_argument(
        "--judge-model",
        help="Model that grades. Defaults to the configured agent model, not --model, "
        "so a weak model under test never grades its own work.",
    )
    parser.add_argument(
        "--budget",
        metavar="TOOL_CALLS,REQUESTS,TOKENS",
        help=f"Override the agent's budget; same format as BUDGET_{name.upper()}",
    )
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args(argv)

    if args.budget:
        if len(args.budget.split(",")) != 3:
            parser.error("--budget takes three fields: TOOL_CALLS,REQUESTS,TOKENS")
        os.environ[f"BUDGET_{name.upper()}"] = args.budget
    if args.model:
        os.environ["AGENT_MODEL"] = args.model

    # A span opened before `logfire.configure()` is never exported.
    load_env()
    configure_logfire()

    dataset = build_dataset(args.judge_model or resolve_agent_model())
    report = dataset.evaluate_sync(
        make_task(), name=name, max_concurrency=args.concurrency
    )
    report.print(
        width=shutil.get_terminal_size((140, 24)).columns,
        include_input=False,
        include_output=False,
        include_reasons=True,
    )
    return 0
