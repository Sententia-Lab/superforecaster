"""CLI entry point for the forecasting agents and the model garden.

Three subcommands, one per agent. All print formatted JSON to stdout.

- forecast: interactive prompts (or --fixture) → run forecast_agent → save to DB
- refresh:  --fixture (in-memory, no DB) or --id (load from DB) → run refresh_agent
- resolve:  --fixture (in-memory, no DB) or --id (load from DB) → run resolution_agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config  # noqa: F401 — loads backend/.env

from . import db
from .agents.critic import run_critique
from .agents.postmortem import run_postmortem
from .agents.resolution import run_resolution_check
from .agents.update import run_update
from .deps import ForecastDeps
from .graphs import (
    forecast_mermaid,
    run_forecast_graph,
    run_update_graph,
    update_mermaid,
)
from .models import (
    Forecast,
    ForecastInput,
    ForecastRecord,
    ForecastUpdateRecord,
    ResearchSummary,
    SubPrediction,
)
from . import model_garden
from .evals import components as component_evals


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _print_json(obj) -> None:
    if hasattr(obj, "model_dump"):
        data = obj.model_dump(mode="json")
    else:
        data = obj
    print(json.dumps(data, indent=2, default=str))


def _load_fixture(path_arg: str | None, default_name: str) -> dict:
    """Resolve --fixture argument to a loaded JSON dict.

    --fixture (no value)   → bundled default in fixtures/
    --fixture <path>       → custom path (relative to cwd)
    """
    if path_arg is None or path_arg == "":
        path = FIXTURES_DIR / default_name
    else:
        path = Path(path_arg)
        if not path.is_absolute() and not path.exists():
            # Try relative to fixtures dir as a fallback
            alt = FIXTURES_DIR / path.name
            if alt.exists():
                path = alt
    if not path.exists():
        print(f"error: fixture not found: {path}", file=sys.stderr)
        sys.exit(2)
    return json.loads(path.read_text())


def _record_from_fixture(data: dict) -> ForecastRecord:
    """Build an in-memory ForecastRecord from a fixture dict.

    Used by `refresh --fixture` and `resolve --fixture` so the agents can
    operate without writing to the DB.
    """
    resolution_date = datetime.fromisoformat(
        data["resolution_date"].replace("Z", "+00:00")
    )
    submission_gap_days = data.get("submission_gap_days", 7)
    submission_deadline = resolution_date.replace(microsecond=0)  # placeholder

    decompositions = [SubPrediction(**d) for d in data.get("decompositions", [])]
    research = ResearchSummary(**data.get("research", {}))
    updates = [
        ForecastUpdateRecord(
            id=str(uuid.uuid4()),
            forecast_id=data["id"],
            probability=u["probability"],
            reasoning=u["reasoning"],
            is_late=u.get("is_late", False),
            created_at=datetime.fromisoformat(u["created_at"].replace("Z", "+00:00")),
        )
        for u in data.get("updates", [])
    ]
    if not updates:
        # Fixture must have at least one update; synthesize from initial reasoning
        updates = [
            ForecastUpdateRecord(
                id=str(uuid.uuid4()),
                forecast_id=data["id"],
                probability=0.5,
                reasoning=data.get("initial_reasoning", ""),
                is_late=False,
                created_at=datetime.now(timezone.utc),
            )
        ]
    return ForecastRecord(
        id=data["id"],
        question=data["question"],
        resolution_criteria=data["resolution_criteria"],
        resolution_source=data["resolution_source"],
        category=data["category"],
        submission_gap_days=submission_gap_days,
        submission_deadline=submission_deadline,
        resolution_date=resolution_date,
        initial_reasoning=data.get("initial_reasoning", ""),
        decompositions=decompositions,
        research=research,
        updates=updates,
        created_at=updates[0].created_at,
    )


# ---------- forecast subcommand ----------


async def _cmd_forecast(args: argparse.Namespace) -> int:
    if args.fixture is not None:
        data = _load_fixture(args.fixture, "forecast_question.json")
        question = data["question"]
        criteria = data["resolution_criteria"]
        source = data["resolution_source"]
        resolution_date = datetime.fromisoformat(
            data["resolution_date"].replace("Z", "+00:00")
        )
        category = data["category"]
    else:
        print("Forecast a new question.")
        question = input("Question: ").strip()
        criteria = input("Resolution criteria: ").strip()
        source = input("Resolution source: ").strip()
        date_str = input("Resolution date (YYYY-MM-DD): ").strip()
        resolution_date = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        category = input("Category: ").strip()

    forecast, violations = await run_forecast_graph(
        ForecastInput(
            question=question,
            resolution_criteria=criteria,
            resolution_date=resolution_date,
            category=category,
            max_iterations=args.max_iterations,
        ),
        verbose=args.verbose,
    )

    if violations:
        print("\n[methodology] this forecast did not satisfy:", file=sys.stderr)
        for v in violations:
            print(f"  P{v.principle} {v.name}: {v.detail}", file=sys.stderr)

    if not args.no_save:
        db.init_db()
        forecast_id = db.save_forecast(forecast, resolution_source=source)
        print(json.dumps({"forecast_id": forecast_id}, indent=2), file=sys.stderr)

    _print_json(forecast)
    return 0


# ---------- refresh subcommand ----------


async def _cmd_refresh(args: argparse.Namespace) -> int:
    if args.id is not None:
        db.init_db()
        _print_json(await run_update_graph(args.id, verbose=args.verbose))
        return 0

    data = _load_fixture(args.fixture, "existing_forecast.json")
    record = _record_from_fixture(data)
    deps = ForecastDeps(verbose=args.verbose)
    _print_json(await run_update(record, deps))
    return 0


# ---------- resolve subcommand ----------


async def _cmd_resolve(args: argparse.Namespace) -> int:
    data = (
        None
        if args.id is not None
        else _load_fixture(args.fixture, "existing_forecast.json")
    )
    if data is None:
        db.init_db()
        record = db.get_forecast(args.id)
        if record is None:
            print(f"forecast {args.id} not found", file=sys.stderr)
            return 1
    else:
        record = _record_from_fixture(data)

    deps = ForecastDeps(verbose=args.verbose)
    _print_json(await run_resolution_check(record, deps))
    return 0


# ---------- critique subcommand ----------


async def _cmd_critique(args: argparse.Namespace) -> int:
    """Principle 3 — is this question resolvable as written?"""
    resolution_date = (
        datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
        if args.date
        else None
    )
    result = await run_critique(
        question=args.question,
        resolution_criteria=args.criteria,
        resolution_date=resolution_date,
        deps=ForecastDeps(verbose=args.verbose),
    )
    _print_json(result)
    return 0


# ---------- postmortem subcommand ----------


async def _cmd_postmortem(args: argparse.Namespace) -> int:
    """Principle 13 — separate process errors from outcome noise."""
    db.init_db()
    record = db.get_forecast(args.id)
    if record is None:
        print(f"forecast {args.id} not found", file=sys.stderr)
        return 1
    _print_json(await run_postmortem(record, ForecastDeps(verbose=args.verbose)))
    return 0


# ---------- models subcommand ----------


async def _cmd_models(args: argparse.Namespace) -> int:
    """Inspect the model garden — clamp 2 of the contamination clamps."""
    if args.action == "probe":
        entries = await model_garden.probe_all()
        print(model_garden.render_garden(entries))
        reach = model_garden.earliest_cutoff()
        if reach is None:
            print("\nNo model is currently available — no clean backtest is possible.")
        else:
            print(f"\nEarliest available training cutoff: {reach.isoformat()}")
            print(
                "A question must be asked after that date (plus the margin) to be clean."
            )
        return 0

    if args.action == "pick":
        as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=timezone.utc)
        entry = model_garden.pick_clean_model(as_of)
        if entry is None:
            print(
                f"No clean model for a question asked {args.as_of} — "
                "every available model was trained after it.",
                file=sys.stderr,
            )
            return 1
        _print_json(entry)
        return 0

    print(model_garden.render_garden(model_garden.list_models(available_only=False)))
    return 0


# ---------- diagram subcommand ----------


async def _cmd_diagram(args: argparse.Namespace) -> int:
    """Render the real graph wiring, so docs cannot drift from code."""
    print(update_mermaid() if args.graph == "update" else forecast_mermaid())
    return 0


# ---------- config subcommand ----------

# Names whose value must never be printed. Everything else is a knob, not a secret.
_SECRETS = {
    "ANTHROPIC_API_KEY",
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "TAVILY_API_KEY",
    "LOGFIRE_TOKEN",
    "ADMIN_API_KEY",
    "OPENAI_API_KEY",
}

_REPORTED = (
    "ANTHROPIC_API_KEY",
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "TAVILY_API_KEY",
    "ADMIN_API_KEY",
    "LOGFIRE_TOKEN",
    "AGENT_MODEL",
    "DATABASE_PATH",
    "CELL_SOFT_CALLS_PER_ITERATION",
    "CELL_HARD_HEADROOM",
)


def _cmd_config(args: argparse.Namespace) -> int:
    """Show every setting and where its value came from.

    `load_dotenv(override=False)` means an exported variable silently beats `backend/.env`,
    and once it has run the two are indistinguishable in `os.environ`. That makes "my .env
    says X, why is it doing Y" unanswerable by inspection — which is the question this
    command exists to answer.

    Secrets are reported as set/unset with a length. The point is provenance, not the value.
    """
    import os

    from config import ENV_FILE, origin, resolve_agent_model

    print(f"\n.env file   {ENV_FILE}  ({'present' if ENV_FILE.exists() else 'ABSENT'})\n")
    print(f"  {'setting':32} {'origin':12} value")
    print(f"  {'-' * 32} {'-' * 12} {'-' * 30}")
    for name in _REPORTED:
        raw = os.getenv(name) or ""
        src = origin(name)
        if not raw:
            shown = "—"
        elif name in _SECRETS:
            shown = f"set ({len(raw)} chars)"
        else:
            shown = raw
        print(f"  {name:32} {src:12} {shown}")

    print()
    try:
        print(f"  resolved model                   {resolve_agent_model()}")
    except RuntimeError as e:
        print(f"  resolved model                   NOT CONFIGURED — {e}")
    return 0


# ---------- serve subcommand ----------


def _cmd_serve(args: argparse.Namespace) -> int:
    """Start the API, which also serves the web UI at `/`.

    Here rather than only as a `uvicorn` incantation because it is the first command
    anyone runs, and `uvicorn api.main:app --port 8000` requires knowing the module path,
    the working directory it resolves against, and that the frontend comes with it.

    Defaults to 127.0.0.1: this binds nothing to the network until someone asks, which is
    also what keeps `api.deps.is_local_mode` an honest statement about who can reach it.

    Synchronous — the only one. `uvicorn.run` builds its own event loop, so dispatching
    this through `asyncio.run` like every other subcommand raises "cannot be called from
    a running event loop". Hence `blocking=True` on the parser defaults.
    """
    import uvicorn

    print(f"\n  http://localhost:{args.port}", flush=True)
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


# ---------- test subcommand ----------


async def _cmd_test(args: argparse.Namespace) -> int:
    """Component tests. The end-to-end backtest lives in spec4.md and is not built."""
    if args.suite != "component":
        print(
            "Only `test component` exists today. The end-to-end backtest over resolved\n"
            "questions is specified in spec/change_specs/spec4.md and is deferred until\n"
            "a corpus of recently-resolved questions is chosen.",
            file=sys.stderr,
        )
        return 2

    agents = component_evals.AGENTS if args.agent in (None, "all") else (args.agent,)
    unknown = [a for a in agents if a not in component_evals.SCORERS]
    if unknown:
        print(f"unknown agent(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    for agent in agents:
        report = await component_evals.run_component(agent, mode=args.mode)
        print(component_evals.render_report(report))
    return 0


# ---------- arg parser ----------


def _add_verbose_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print agent tool calls and usage stats to stderr",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superforecaster")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_forecast = sub.add_parser("forecast", help="Run the forecast agent")
    p_forecast.add_argument(
        "--fixture",
        nargs="?",
        const="",
        default=None,
        help="Load input from a fixture JSON file (default: bundled)",
    )
    p_forecast.add_argument(
        "--no-save", action="store_true", help="Don't save the result to SQLite"
    )
    p_forecast.add_argument("--max-iterations", type=int, default=5)
    _add_verbose_flag(p_forecast)
    p_forecast.set_defaults(func=_cmd_forecast)

    p_refresh = sub.add_parser("refresh", help="Run the refresh agent")
    grp_r = p_refresh.add_mutually_exclusive_group(required=True)
    grp_r.add_argument(
        "--fixture",
        nargs="?",
        const="",
        default=None,
        help="Load forecast from a fixture file (in-memory, no DB write)",
    )
    grp_r.add_argument("--id", help="Refresh a forecast by UUID from the DB")
    _add_verbose_flag(p_refresh)
    p_refresh.set_defaults(func=_cmd_refresh)

    p_resolve = sub.add_parser("resolve", help="Run the resolution agent")
    grp_v = p_resolve.add_mutually_exclusive_group(required=True)
    grp_v.add_argument(
        "--fixture",
        nargs="?",
        const="",
        default=None,
        help="Load forecast from a fixture file (in-memory, no DB write)",
    )
    grp_v.add_argument(
        "--id", help="Check resolution for a forecast by UUID from the DB"
    )
    _add_verbose_flag(p_resolve)
    p_resolve.set_defaults(func=_cmd_resolve)

    p_critique = sub.add_parser(
        "critique", help="Check whether a question is resolvable"
    )
    p_critique.add_argument("--question", required=True)
    p_critique.add_argument("--criteria", required=True)
    p_critique.add_argument("--date", help="Proposed resolution date (YYYY-MM-DD)")
    _add_verbose_flag(p_critique)
    p_critique.set_defaults(func=_cmd_critique)

    p_post = sub.add_parser(
        "postmortem", help="Review a resolved forecast for process errors"
    )
    p_post.add_argument("id", help="Forecast UUID")
    _add_verbose_flag(p_post)
    p_post.set_defaults(func=_cmd_postmortem)

    p_models = sub.add_parser("models", help="Inspect the model garden")
    p_models.add_argument(
        "action", nargs="?", default="list", choices=["list", "probe", "pick"]
    )
    p_models.add_argument("--as-of", help="Date to pick a clean model for (YYYY-MM-DD)")
    _add_verbose_flag(p_models)
    p_models.set_defaults(func=_cmd_models)

    p_diagram = sub.add_parser("diagram", help="Print the graph as mermaid")
    p_diagram.add_argument(
        "graph", nargs="?", default="forecast", choices=["forecast", "update"]
    )
    _add_verbose_flag(p_diagram)
    p_diagram.set_defaults(func=_cmd_diagram)

    p_test = sub.add_parser("test", help="Run the component eval harness")
    p_test.add_argument(
        "suite", nargs="?", default="component", choices=["component", "e2e"]
    )
    p_test.add_argument(
        "agent", nargs="?", default="all", help="Agent name, or 'all' (default)"
    )
    p_test.add_argument(
        "--mode",
        default="clean",
        choices=["clean", "production"],
        help="clean picks a model trained before the case's as_of",
    )
    _add_verbose_flag(p_test)
    p_test.set_defaults(func=_cmd_test)

    p_config = sub.add_parser(
        "config", help="Show every setting and where its value came from"
    )
    p_config.set_defaults(func=_cmd_config, blocking=True)

    p_serve = sub.add_parser("serve", help="Start the API and the web UI")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--reload", action="store_true", help="Restart on file changes")
    p_serve.set_defaults(func=_cmd_serve, blocking=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # `serve` runs its own event loop and so cannot be awaited inside one.
    if getattr(args, "blocking", False):
        return args.func(args)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
