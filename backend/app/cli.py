"""The `superforecaster` console script. Every command prints JSON to stdout."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
import typer

from superforecaster.agents.critic import run_critique
from superforecaster.agents.postmortem import run_postmortem
from superforecaster.agents.resolution import run_resolution_check
from superforecaster.agents.update import run_update
from superforecaster.config import resolve_agent_model
from superforecaster.deps import ForecastDeps
from superforecaster.models import ForecastInput, ForecastRecord
from superforecaster.stages import run_all

from . import db, research
from .config import ENV_FILE, load_env, origin
from .observability import configure_logfire
from .update import run_update_graph

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

app = typer.Typer(
    name="superforecaster",
    help="Forecast, refresh, resolve, and inspect.",
    no_args_is_help=True,
    add_completion=False,
)

VERBOSE = typer.Option(False, "-v", "--verbose", help="Print agent activity")


def _print_json(obj) -> None:
    data = obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj
    print(json.dumps(data, indent=2, default=str))


def _load_fixture(path_arg: str | None, default_name: str) -> dict:
    """`--fixture` loads the bundled file; `--fixture-path` loads any JSON file."""
    path = Path(path_arg) if path_arg else FIXTURES_DIR / default_name
    if not path.exists():
        print(f"error: fixture not found: {path}", file=sys.stderr)
        sys.exit(2)
    return json.loads(path.read_text())


def _record_from_fixture(data: dict) -> ForecastRecord:
    """An in-memory `ForecastRecord`, so `refresh` and `resolve` can run without the DB."""
    now = datetime.now(timezone.utc)
    updates = data.get("updates") or [
        {
            "probability": 0.5,
            "reasoning": data.get("initial_reasoning", ""),
            "created_at": now,
        }
    ]
    return ForecastRecord.model_validate(
        {
            "submission_gap_days": 7,
            "submission_deadline": data["resolution_date"],
            "initial_reasoning": "",
            "created_at": updates[0]["created_at"],
            **data,
            "updates": [
                {
                    "id": str(uuid.uuid4()),
                    "forecast_id": data["id"],
                    "is_late": False,
                    **u,
                }
                for u in updates
            ],
        }
    )


def _record(fixture: str | None, id: str | None, what: str) -> ForecastRecord:
    """The forecast a command works on: from a fixture or from the DB by `--id`."""
    if (fixture is None) == (id is None):
        raise typer.BadParameter(f"give exactly one of --fixture or --id to {what}")
    if id is not None:
        db.init_db()
        record = db.get_forecast(id)
        if record is None:
            print(f"forecast {id} not found", file=sys.stderr)
            raise typer.Exit(1)
        return record
    return _record_from_fixture(_load_fixture(fixture, "existing_forecast.json"))


def _deps(record: ForecastRecord) -> ForecastDeps:
    return ForecastDeps(store=research.store_for(record.research_id))


@app.command()
def forecast(
    fixture: bool = typer.Option(False, "--fixture", help="Use the bundled question"),
    fixture_path: str = typer.Option(None, help="Load the question from a JSON file"),
    no_save: bool = typer.Option(False, "--no-save", help="Do not save to SQLite"),
    max_iterations: int = 5,
    verbose: bool = VERBOSE,
) -> None:
    """Run every stage back-to-back and save the forecast."""
    if fixture or fixture_path:
        data = _load_fixture(fixture_path, "forecast_question.json")
    else:
        print("Forecast a new question.")
        data = {
            "question": input("Question: ").strip(),
            "resolution_criteria": input("Resolution criteria: ").strip(),
            "resolution_source": input("Resolution source: ").strip(),
            "resolution_date": input("Resolution date (YYYY-MM-DD): ").strip(),
            "category": input("Category: ").strip(),
        }
    input_ = ForecastInput(**{**data, "max_iterations": max_iterations})
    db.init_db()  # the research store lives in the database even when nothing is saved
    store = research.new_store()
    result, violations = asyncio.run(run_all(input_, store=store))

    for v in violations:
        print(f"[methodology] P{v.principle} {v.name}: {v.detail}", file=sys.stderr)
    if not no_save:
        fid = db.save_forecast(
            result,
            resolution_source=data["resolution_source"],
            research_id=store.research_id,
        )
        print(json.dumps({"forecast_id": fid}), file=sys.stderr)
    _print_json(result)


@app.command()
def refresh(
    fixture: bool = typer.Option(False, "--fixture", help="Use the bundled forecast"),
    fixture_path: str = typer.Option(None, help="Load a forecast from a JSON file"),
    id: str = typer.Option(None, help="Forecast UUID in the DB"),
    verbose: bool = VERBOSE,
) -> None:
    """Check one forecast for resolution, then update it against new evidence."""
    if id is not None and not (fixture or fixture_path):
        db.init_db()
        _print_json(asyncio.run(run_update_graph(id)))
        return
    record = _record(fixture_path or ("" if fixture else None), id, "refresh")
    _print_json(asyncio.run(run_update(record, _deps(record))))


@app.command()
def resolve(
    fixture: bool = typer.Option(False, "--fixture", help="Use the bundled forecast"),
    fixture_path: str = typer.Option(None, help="Load a forecast from a JSON file"),
    id: str = typer.Option(None, help="Forecast UUID in the DB"),
    verbose: bool = VERBOSE,
) -> None:
    """Ask whether a forecast has already resolved."""
    record = _record(fixture_path or ("" if fixture else None), id, "resolve")
    _print_json(asyncio.run(run_resolution_check(record, _deps(record))))


@app.command()
def critique(
    question: str = typer.Option(..., help="The question text"),
    criteria: str = typer.Option(..., help="Its resolution criteria"),
    date: str = typer.Option(None, help="Proposed resolution date (YYYY-MM-DD)"),
    verbose: bool = VERBOSE,
) -> None:
    """Check whether a question is resolvable. Principle 3."""
    resolution_date = (
        datetime.fromisoformat(date).replace(tzinfo=timezone.utc) if date else None
    )
    _print_json(
        asyncio.run(
            run_critique(
                question=question,
                resolution_criteria=criteria,
                resolution_date=resolution_date,
            )
        )
    )


@app.command()
def postmortem(
    id: str = typer.Argument(..., help="Forecast UUID"), verbose: bool = VERBOSE
) -> None:
    """Separate process errors from outcome noise on a resolved forecast. Principle 13."""
    record = _record(None, id, "postmortem")
    _print_json(asyncio.run(run_postmortem(record, _deps(record))))


_FORECAST_MERMAID = """\
stateDiagram-v2
    [*] --> decompose
    decompose --> lenses : gate (next)
    lenses --> base_rates : gate (all sub-questions have lenses)
    base_rates --> inside_view : gate (all lenses measured)
    inside_view --> synthesis : gate (all rates adjusted)
    synthesis --> [*]
"""

_UPDATE_MERMAID = """\
stateDiagram-v2
    [*] --> resolution_check
    resolution_check --> [*] : resolved, flagged for review
    resolution_check --> update : not resolved
    update --> verify_large_move : |delta| > CHECK_LARGE_MOVE
    verify_large_move --> gate
    update --> gate
    gate --> [*] : noise, inconsistent, or written
"""


@app.command()
def diagram(graph: str = typer.Argument("forecast", help="forecast | update")) -> None:
    """Print a pipeline as mermaid."""
    if graph not in ("forecast", "update"):
        raise typer.BadParameter("graph must be forecast or update")
    print(_UPDATE_MERMAID if graph == "update" else _FORECAST_MERMAID)


_SECRETS = {
    "ANTHROPIC_API_KEY",
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "TAVILY_API_KEY",
    "LOGFIRE_TOKEN",
}
_REPORTED = (
    *_SECRETS,
    "AGENT_MODEL",
    "DATABASE_PATH",
    "BUDGET_BASE_RATE_CELL",
    "BUDGET_INSIDE_VIEW",
)


@app.command()
def config() -> None:
    """Show every setting and where its value came from. Secrets are shown as set/unset."""
    print(
        f"\n.env file   {ENV_FILE}  ({'present' if ENV_FILE.exists() else 'ABSENT'})\n"
    )
    print(f"  {'setting':32} {'origin':12} value")
    for name in _REPORTED:
        raw = os.getenv(name) or ""
        shown = (
            "—" if not raw else f"set ({len(raw)} chars)" if name in _SECRETS else raw
        )
        print(f"  {name:32} {origin(name):12} {shown}")
    print()
    try:
        print(f"  resolved model                   {resolve_agent_model()}")
    except RuntimeError as e:
        print(f"  resolved model                   NOT CONFIGURED — {e}")


@app.command()
def serve(
    port: int = 8000,
    host: str = "127.0.0.1",
    reload: bool = typer.Option(False, help="Restart on file changes"),
) -> None:
    """Start the API and the web UI."""
    import uvicorn

    print(f"\n  http://localhost:{port}", flush=True)
    uvicorn.run("api.main:app", host=host, port=port, reload=reload, log_level="info")


def main(argv: list[str] | None = None) -> int:
    load_env()
    # Configure before typer dispatches, so the first agent run is already traced.
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
