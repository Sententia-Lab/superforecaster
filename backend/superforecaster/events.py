"""What an agent reports while it is working.

An agent run produces more than its return value: the searches it made, the sources it
found, the text it narrated. A caller that wants to show progress subscribes by putting
a `Sink` on `ForecastDeps.emit`.

These are typed values rather than the dictionaries a particular frontend happens to
want. The wire format is the application's business — `app.stream` turns each of these
into the shape the browser reads. Core describes what happened; the app decides how to
say it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import SourceRef


@dataclass(frozen=True)
class Query:
    """A tool call, as the question it is asking."""

    tool: str
    text: str


@dataclass(frozen=True)
class Source:
    """One source a tool returned. Carries the full ref, including its leak status."""

    ref: SourceRef


@dataclass(frozen=True)
class Thought:
    """A fragment of model narration, streamed as it arrives."""

    delta: str


@dataclass(frozen=True)
class Exhausted:
    """A research cell stopped because its budget ran out, not because it was done."""


AgentEvent = Query | Source | Thought | Exhausted

Sink = Callable[[AgentEvent, "str | None"], None]
"""Called as `sink(event, sub_question)`.

`sub_question` is which column of the grid the event belongs to, or None for work done
by the run as a whole. It is an opaque tag here — core never interprets it.

MUST be synchronous and non-blocking: it is called from inside the agent's own event
stream, and awaiting there would stall token delivery.
"""
