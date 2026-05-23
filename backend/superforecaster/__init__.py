"""Superforecaster — crowd-sourced forecasting platform."""

import config  # noqa: F401 — loads backend/.env before agent modules import

from .observability import configure_logfire

configure_logfire()

__version__ = "0.3.0"
