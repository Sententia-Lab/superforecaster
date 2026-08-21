"""CLI entry point for the forecasting agents and the model garden.

All print formatted JSON to stdout.

- forecast: interactive prompts (or --fixture) → `stages.run_all` (every stage
  back-to-back, no gates — the gated flow is the web UI's) → save to DB
- refresh:  --fixture (in-memory, no DB) or --id (load from DB) → the update graph
- resolve:  --fixture (in-memory, no DB) or --id (load from DB) → resolution check
"""

from __future__ import annotations

from types import SimpleNamespace

import click
import typer

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import load_env
from .observability import configure_logfire

from . import db, research
from superforecaster.agents.critic import run_critique
from superforecaster.agents.postmortem import run_postmortem
from superforecaster.agents.resolution import run_resolution_check
from superforecaster.agents.update import run_update
from superforecaster.deps import ForecastDeps
from .update import run_update_graph

from superforecaster.update import update_mermaid
from superforecaster.stages import run_all
from superforecaster.models import (
    Forecast,
    ForecastInput,
    ForecastRecord,
    ForecastUpdateRecord,
    ResearchSummary,
    SubPrediction,
)
from superforecaster import model_garden

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

    store = research.new_store()
    forecast, violations = await run_all(
        ForecastInput(
            question=question,
            resolution_criteria=criteria,
            resolution_date=resolution_date,
            category=category,
            max_iterations=args.max_iterations,
        ),
        store=store,
    )

    if violations:
        print("\n[methodology] this forecast did not satisfy:", file=sys.stderr)
        for v in violations:
            print(f"  P{v.principle} {v.name}: {v.detail}", file=sys.stderr)

    if not args.no_save:
        db.init_db()
        forecast_id = db.save_forecast(
            forecast, resolution_source=source, research_id=store.research_id
        )
        print(json.dumps({"forecast_id": forecast_id}, indent=2), file=sys.stderr)

    _print_json(forecast)
    return 0


# ---------- refresh subcommand ----------


async def _cmd_refresh(args: argparse.Namespace) -> int:
    if args.id is not None:
        db.init_db()
        _print_json(await run_update_graph(args.id))
        return 0

    data = _load_fixture(args.fixture, "existing_forecast.json")
    record = _record_from_fixture(data)
    deps = ForecastDeps(store=research.store_for(record.research_id))
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

    deps = ForecastDeps(store=research.store_for(record.research_id))
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
        deps=ForecastDeps(),
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
    _print_json(
        await run_postmortem(
            record, ForecastDeps(store=research.store_for(record.research_id))
        )
    )
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
        forecast_date = datetime.fromisoformat(args.forecast_date).replace(
            tzinfo=timezone.utc
        )
        entry = model_garden.pick_clean_model(forecast_date)
        if entry is None:
            print(
                f"No clean model for a question asked {args.forecast_date} — "
                "every available model was trained after it.",
                file=sys.stderr,
            )
            return 1
        _print_json(entry)
        return 0

    print(model_garden.render_garden(model_garden.list_models(available_only=False)))
    return 0


# ---------- diagram subcommand ----------

_FORECAST_MERMAID = """\
stateDiagram-v2
    [*] --> decompose
    decompose --> lenses : gate (next)
    lenses --> base_rates : gate (all sub-questions have lenses)
    base_rates --> inside_view : gate (all lenses measured)
    inside_view --> synthesis : gate (all rates adjusted)
    synthesis --> [*]
"""


async def _cmd_diagram(args: argparse.Namespace) -> int:
    """Render the pipeline shape. The update graph is generated from real wiring;
    the forecast pipeline is a gated state machine, so its diagram is the machine's
    stage table rather than graph edges."""
    print(update_mermaid() if args.graph == "update" else _FORECAST_MERMAID)
    return 0


# ---------- config subcommand ----------

# Names whose value must never be printed. Everything else is a knob, not a secret.
_SECRETS = {
    "ANTHROPIC_API_KEY",
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "TAVILY_API_KEY",
    "LOGFIRE_TOKEN",
    "OPENAI_API_KEY",
}

_REPORTED = (
    "ANTHROPIC_API_KEY",
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "TAVILY_API_KEY",
    "LOGFIRE_TOKEN",
    "AGENT_MODEL",
    "DATABASE_PATH",
    "BUDGET_BASE_RATE_CELL",
    "BUDGET_INSIDE_VIEW",
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

    from .config import ENV_FILE, origin

    from superforecaster.config import resolve_agent_model

    print(
        f"\n.env file   {ENV_FILE}  ({'present' if ENV_FILE.exists() else 'ABSENT'})\n"
    )
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

    Defaults to 127.0.0.1: this binds nothing to the network until someone asks.

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


# ---------- arg parser ----------


app = typer.Typer(
    name="superforecaster",
    help="Forecast, refresh, resolve, and inspect.",
    no_args_is_help=True,
    add_completion=False,
)

VERBOSE = typer.Option(
    False, "-v", "--verbose", help="Print agent activity to the terminal"
)


def _run(fn, **kwargs) -> None:
    """Drive one command. Every `_cmd_*` still takes a namespace of flags.

    Typer replaces `_build_parser` — a hundred lines that restated each command's
    signature a second time, in a second syntax, next to the function that already
    declared it. The command bodies are untouched; they read their flags off an object,
    and a `SimpleNamespace` is that object.
    """
    code = asyncio.run(fn(SimpleNamespace(**kwargs)))
    raise typer.Exit(code or 0)


def _run_blocking(fn, **kwargs) -> None:
    """`serve` and `config` run their own event loop, so they cannot be awaited in one."""
    raise typer.Exit(fn(SimpleNamespace(**kwargs)) or 0)


def _one_of(fixture: str | None, id: str | None, what: str) -> None:
    """`--fixture` and `--id` are alternatives, which argparse enforced structurally."""
    if (fixture is None) == (id is None):
        raise typer.BadParameter(f"give exactly one of --fixture or --id to {what}")


@app.command()
def forecast(
    fixture: str = typer.Option(None, help="Load input from a fixture JSON file"),
    no_save: bool = typer.Option(False, "--no-save", help="Do not save to SQLite"),
    max_iterations: int = 5,
    verbose: bool = VERBOSE,
) -> None:
    """Run the forecast agent."""
    _run(
        _cmd_forecast,
        fixture=fixture,
        no_save=no_save,
        max_iterations=max_iterations,
        verbose=verbose,
    )


@app.command()
def refresh(
    fixture: str = typer.Option(
        None, help="Load forecast from a fixture (no DB write)"
    ),
    id: str = typer.Option(None, help="Refresh a forecast by UUID from the DB"),
    verbose: bool = VERBOSE,
) -> None:
    """Run the refresh agent."""
    _one_of(fixture, id, "refresh")
    _run(_cmd_refresh, fixture=fixture, id=id, verbose=verbose)


@app.command()
def resolve(
    fixture: str = typer.Option(
        None, help="Load forecast from a fixture (no DB write)"
    ),
    id: str = typer.Option(None, help="Check resolution by UUID from the DB"),
    verbose: bool = VERBOSE,
) -> None:
    """Run the resolution agent."""
    _one_of(fixture, id, "resolve")
    _run(_cmd_resolve, fixture=fixture, id=id, verbose=verbose)


@app.command()
def critique(
    question: str = typer.Option(..., help="The question text"),
    criteria: str = typer.Option(..., help="Its resolution criteria"),
    date: str = typer.Option(None, help="Proposed resolution date (YYYY-MM-DD)"),
    verbose: bool = VERBOSE,
) -> None:
    """Check whether a question is resolvable."""
    _run(
        _cmd_critique, question=question, criteria=criteria, date=date, verbose=verbose
    )


@app.command()
def postmortem(
    id: str = typer.Argument(..., help="Forecast UUID"), verbose: bool = VERBOSE
) -> None:
    """Review a resolved forecast for process errors."""
    _run(_cmd_postmortem, id=id, verbose=verbose)


@app.command()
def models(
    action: str = typer.Argument("list", help="list | probe | pick"),
    forecast_date: str = typer.Option(
        None, help="Date to pick a clean model for (YYYY-MM-DD)"
    ),
    verbose: bool = VERBOSE,
) -> None:
    """Inspect the model garden."""
    if action not in ("list", "probe", "pick"):
        raise typer.BadParameter("action must be list, probe, or pick")
    _run(_cmd_models, action=action, forecast_date=forecast_date, verbose=verbose)


@app.command()
def diagram(
    graph: str = typer.Argument("forecast", help="forecast | update"),
    verbose: bool = VERBOSE,
) -> None:
    """Print the graph as mermaid."""
    if graph not in ("forecast", "update"):
        raise typer.BadParameter("graph must be forecast or update")
    _run(_cmd_diagram, graph=graph, verbose=verbose)


@app.command()
def config() -> None:
    """Show every setting and where its value came from."""
    _run_blocking(_cmd_config)


@app.command()
def serve(
    port: int = 8000,
    host: str = "127.0.0.1",
    reload: bool = typer.Option(False, help="Restart on file changes"),
) -> None:
    """Start the API and the web UI."""
    _run_blocking(_cmd_serve, port=port, host=host, reload=reload)


def main(argv: list[str] | None = None) -> int:
    """Kept as a function so the console script and the tests share an entry."""
    load_env()
    # The library instruments; this process decides where the traces go. Verbose is read
    # off argv rather than the parsed flags because configuration has to happen before
    # the first agent runs, and typer has not dispatched yet.
    configure_logfire(verbose=bool({"-v", "--verbose"} & set(argv or sys.argv[1:])))
    try:
        app(args=argv, standalone_mode=False)
    except typer.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
