"""Logfire configuration, and the terminal progress printer.

The core library emits spans and logs but never configures Logfire — deciding whether
traces leave this process, and where they go, is the application's call. This module
makes it, once, at each entry point.

The token is probed over HTTP before use. An invalid one silently sends nothing, and a
run that produced no trace looks exactly like a run nobody looked at.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx
import logfire

from superforecaster.config import get_settings

_logfire_configured = False
_warned_invalid_logfire_token = False
_cloud_tracing_active = False
_console_active = False


def _looks_like_logfire_write_token(token: str) -> bool:
    return token.startswith("pylf_v1_")


def logfire_tracing_enabled() -> bool:
    token = (get_settings().logfire_token or "").strip()
    return _looks_like_logfire_write_token(token)


def cloud_tracing_active() -> bool:
    return _cloud_tracing_active


def console_active() -> bool:
    return _console_active


def _logfire_base_url(token: str) -> str:
    parts = token.split("_")
    region = parts[2] if len(parts) > 2 else "us"
    return f"https://logfire-{region}.pydantic.dev"


def _token_is_valid(token: str) -> bool:
    try:
        response = httpx.get(
            f"{_logfire_base_url(token)}/v1/info",
            headers={"Authorization": token},
            timeout=5.0,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def configure_logfire(*, verbose: bool = False) -> None:
    """Point Logfire at the cloud, the console, or nowhere. Idempotent.

    The console is switched on whenever cloud tracing is off, so a run always reports
    itself somewhere. `verbose` turns it on as well even when traces are being sent.
    """
    global _logfire_configured, _warned_invalid_logfire_token, _cloud_tracing_active, _console_active
    if _logfire_configured:
        return

    settings = get_settings()
    token = (settings.logfire_token or "").strip()
    tracing = logfire_tracing_enabled() and _token_is_valid(token)

    if not tracing and not _warned_invalid_logfire_token:
        _warned_invalid_logfire_token = True
        print(
            "[logfire] Cloud tracing unavailable (no valid write token) — printing agent logs to the "
            "terminal instead. Create a write token at logfire.pydantic.dev → project → Settings → "
            "Write tokens and set LOGFIRE_TOKEN to enable cloud traces.",
            file=sys.stderr,
            flush=True,
        )

    kwargs: dict[str, Any] = {
        "service_name": "superforecaster",
        "scrubbing": False,
        "send_to_logfire": tracing,
        "console": (
            logfire.ConsoleOptions(min_log_level="info")
            if (verbose or not tracing)
            else False
        ),
    }
    if tracing:
        kwargs["token"] = token

    logfire.configure(**kwargs)
    # Unconditionally, not only when traces are sent to the cloud. This is what records
    # tool calls and model messages, and with the console on and no token it is the only
    # thing that does — `runner` builds its event handler for a UI subscriber, not for
    # tracing, so a verbose CLI run with no token would otherwise report nothing.
    logfire.instrument_pydantic_ai(include_content=True, version=3)

    _cloud_tracing_active = tracing
    _console_active = bool(kwargs["console"])
    _logfire_configured = True
