"""Superforecaster — crowd-sourced forecasting platform."""

from pathlib import Path

from dotenv import load_dotenv

# Load .env from backend/ first, fall back to repo root.
# Safe no-op if files don't exist. Must happen before agent modules import.
_pkg_root = Path(__file__).resolve().parent.parent
load_dotenv(_pkg_root / ".env", override=False)
load_dotenv(_pkg_root.parent / ".env", override=False)

__version__ = "0.3.0"
