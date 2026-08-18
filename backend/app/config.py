"""Deployment configuration — the settings only a running service needs.

Split from `superforecaster.config`, which holds the tunables the forecasting logic
reads. The line between them is whether core could work without it: core has no
database and no frontend, so those live here.

This module also owns `.env` loading. Reading a file off disk at import time is a thing
an application may do and a library may not, so `load_env()` is explicit and every
entry point calls it once.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_ROOT / ".env"

# Snapshot which names the real environment already carried, BEFORE any file is read.
# `override=False` means an exported variable beats `.env`, and once `load_dotenv` has
# run the two are indistinguishable in `os.environ` — so "why is this setting not what my
# .env says" becomes unanswerable unless the answer is captured here, first.
_PRESET_ENV: frozenset[str] = frozenset(k for k, v in os.environ.items() if v != "")

_env_loaded = False


def load_env(path: Path = ENV_FILE) -> None:
    """Read `backend/.env` into the environment. Exported variables win.

    Every entry point calls this before it touches settings — the API app, the CLI, and
    each eval script. Calling it twice is harmless; calling it never means the process
    runs on exported variables alone, which is what a container does.
    """
    global _env_loaded
    if _env_loaded:
        return
    load_dotenv(path, override=False)
    _env_loaded = True


RUNTIME_KEYS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "PYDANTIC_AI_GATEWAY_API_KEY",
        "TAVILY_API_KEY",
        "WIKIPEDIA_API_KEY",
    }
)
"""The only names `set_runtime_key` will write. ADR 61.

The allowlist is the whole safety of the key panel. Without it the endpoint writes any
name into `os.environ`, and `DATABASE_PATH` and `FRONTEND_DIR` are both read from there —
an admin-authenticated way to repoint the database is not a key panel.
"""

_RUNTIME_SET: set[str] = set()
"""Names set through the panel this process, so `origin` can tell the truth about them."""


def set_runtime_key(name: str, value: str) -> None:
    """Set or clear one allowlisted key for the life of this process.

    Nothing is written to disk. Both settings getters re-read `os.environ` on every call,
    so the next request sees this with no cache to invalidate and no restart.
    """
    if name not in RUNTIME_KEYS:
        raise ValueError(f"{name} is not a runtime-settable key")
    if value:
        os.environ[name] = value
        _RUNTIME_SET.add(name)
    else:
        os.environ.pop(name, None)
        _RUNTIME_SET.discard(name)


def origin(name: str) -> str:
    """Where `name`'s value came from: this session, the environment, `.env`, or nowhere.

    `session` exists because a runtime-set key otherwise reports `.env` — a lie about
    provenance in the one place a reader goes to check provenance.
    """
    if name in _RUNTIME_SET:
        return "session"
    if name in _PRESET_ENV:
        return "environment"
    if os.getenv(name):
        return ".env"
    return "unset"


@dataclass(frozen=True, slots=True)
class AppSettings:
    database_path: str
    refresh_cron_schedule: str
    frontend_dir: str


def get_app_settings() -> AppSettings:
    """Re-read on every call, matching `superforecaster.config.get_settings`."""
    return AppSettings(
        database_path=os.getenv("DATABASE_PATH", "./superforecaster.db"),
        refresh_cron_schedule=os.getenv("REFRESH_CRON_SCHEDULE", "0 6 * * *"),
        frontend_dir=os.getenv(
            "FRONTEND_DIR", str(BACKEND_ROOT.parent / "frontend" / "dist")
        ),
    )
