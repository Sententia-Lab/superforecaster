"""CLI entry point: `uv run python -m superforecaster {forecast|refresh|resolve}`.

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
from .agent import run_forecast
from .models import (
    Forecast,
    ForecastInput,
    ForecastRecord,
    ForecastUpdateRecord,
    ResearchSummary,
    SubPrediction,
)
from .refresh import refresh_forecast, run_refresh_agent
from .resolution import check_resolution, run_resolution_agent


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
    resolution_date = datetime.fromisoformat(data["resolution_date"].replace("Z", "+00:00"))
    submission_gap_days = data.get("submission_gap_days", 7)
    submission_deadline = resolution_date.replace(microsecond=0)  # placeholder

    decompositions = [SubPrediction(**d) for d in data.get("decompositions", [])]
    research = ResearchSummary(**data.get("research", {}))
    updates = [
        ForecastUpdateRecord(
            id=str(uuid.uuid4()),
            forecast_id=data["id"],
            probability=u["probability"],
            confidence=u["confidence"],
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
                confidence="medium",
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
        resolution_date = datetime.fromisoformat(data["resolution_date"].replace("Z", "+00:00"))
        category = data["category"]
    else:
        print("Forecast a new question.")
        question = input("Question: ").strip()
        criteria = input("Resolution criteria: ").strip()
        source = input("Resolution source: ").strip()
        date_str = input("Resolution date (YYYY-MM-DD): ").strip()
        resolution_date = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        category = input("Category: ").strip()

    forecast: Forecast = await run_forecast(
        ForecastInput(
            question=question,
            resolution_criteria=criteria,
            resolution_date=resolution_date,
            category=category,
            max_iterations=args.max_iterations,
        )
    )

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
        result = await refresh_forecast(args.id)
        _print_json(result)
        return 0

    data = _load_fixture(args.fixture, "existing_forecast.json")
    record = _record_from_fixture(data)
    result = await run_refresh_agent(record)
    _print_json(result)
    return 0


# ---------- resolve subcommand ----------


async def _cmd_resolve(args: argparse.Namespace) -> int:
    if args.id is not None:
        db.init_db()
        result = await check_resolution(args.id)
        _print_json(result)
        return 0

    data = _load_fixture(args.fixture, "existing_forecast.json")
    record = _record_from_fixture(data)
    result = await run_resolution_agent(record)
    _print_json(result)
    return 0


# ---------- arg parser ----------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superforecaster")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_forecast = sub.add_parser("forecast", help="Run the forecast agent")
    p_forecast.add_argument("--fixture", nargs="?", const="", default=None,
                            help="Load input from a fixture JSON file (default: bundled)")
    p_forecast.add_argument("--no-save", action="store_true",
                            help="Don't save the result to SQLite")
    p_forecast.add_argument("--max-iterations", type=int, default=5)
    p_forecast.set_defaults(func=_cmd_forecast)

    p_refresh = sub.add_parser("refresh", help="Run the refresh agent")
    grp_r = p_refresh.add_mutually_exclusive_group(required=True)
    grp_r.add_argument("--fixture", nargs="?", const="", default=None,
                       help="Load forecast from a fixture file (in-memory, no DB write)")
    grp_r.add_argument("--id", help="Refresh a forecast by UUID from the DB")
    p_refresh.set_defaults(func=_cmd_refresh)

    p_resolve = sub.add_parser("resolve", help="Run the resolution agent")
    grp_v = p_resolve.add_mutually_exclusive_group(required=True)
    grp_v.add_argument("--fixture", nargs="?", const="", default=None,
                       help="Load forecast from a fixture file (in-memory, no DB write)")
    grp_v.add_argument("--id", help="Check resolution for a forecast by UUID from the DB")
    p_resolve.set_defaults(func=_cmd_resolve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
