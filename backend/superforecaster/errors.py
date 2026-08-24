"""Timeouts this system raises on purpose. Both subclass `TimeoutError`."""

from __future__ import annotations


class AgentTimeout(TimeoutError):
    """One agent run exceeded `AGENT_TIMEOUT_SECONDS`: the model stopped responding."""


class StageTimeout(TimeoutError):
    """One gated stage step exceeded `STAGE_TIMEOUT_SECONDS`."""
