"""Superforecaster — the forecasting methodology as an importable library.

Start here:

    from superforecaster import ForecastInput, run_all

    forecast, violations = await run_all(ForecastInput(...))

`run_all` drives the whole pipeline and returns the forecast plus any methodology
violations that survived its retry, so a caller can tell a clean forecast from one that
never satisfied its own checks.

To drive one stage at a time, call the functions in `superforecaster.stages` yourself,
passing a `ForecastDeps` — that is what carries the backtest clamps (`as_of`, `model`)
and the progress sink. To reach a single agent, call its `run_*` seam in
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
