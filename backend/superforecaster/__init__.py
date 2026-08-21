"""Superforecaster — the forecasting methodology as an importable library.

    from superforecaster import ForecastInput, run_all
    forecast, violations = await run_all(ForecastInput(...))

Importing this package has no side effects: no files, no sockets, no logging.
"""

from .deps import ForecastDeps
from .models import Forecast, ForecastInput
from .stages import run_all

__version__ = "0.3.0"

__all__ = ["Forecast", "ForecastDeps", "ForecastInput", "run_all", "__version__"]
