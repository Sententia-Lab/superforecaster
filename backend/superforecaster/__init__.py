"""Superforecaster — the forecasting methodology as an importable library.

Start here:

    from superforecaster import ForecastDeps, ForecastInput, run_all

    forecast = await run_all(ForecastInput(...), ForecastDeps())

`run_all` runs the whole pipeline. To drive one step at a time, call the functions in
`superforecaster.stages` yourself; to reach a single agent, call its `run_*` seam in
`superforecaster.agents`.

Importing this package has no side effects. It reads no files, opens no connections,
and configures no logging — `tests/test_layering.py` proves it. An application that
wants tracing calls `logfire.configure()` itself; this library only emits spans.
"""

from .deps import ForecastDeps
from .models import Forecast, ForecastInput
from .stages import run_all

__version__ = "0.3.0"

__all__ = ["Forecast", "ForecastDeps", "ForecastInput", "run_all", "__version__"]
