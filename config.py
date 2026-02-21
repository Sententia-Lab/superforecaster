"""
Central configuration - loads .env once at import time.

Import this module early (e.g. at the top of your main script) to ensure
environment variables from .env are available before any os.getenv() calls.
"""

from pathlib import Path

from dotenv import load_dotenv

# Load from project root so it works regardless of cwd
load_dotenv(Path(__file__).resolve().parent / ".env")
