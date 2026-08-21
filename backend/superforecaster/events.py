"""What an agent reports while it works, and the SSE frame each event becomes.

A caller subscribes by putting a `Sink` on `ForecastDeps.emit`. The field names in
`frame` are read by `frontend/src/hooks/useStepStream.js`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .models import SourceRef


@dataclass(frozen=True)
class Query:
    """A tool call, as the question it is asking."""

    tool: str
    text: str


@dataclass(frozen=True)
class Source:
    """One source a tool returned."""

    ref: SourceRef


@dataclass(frozen=True)
class Thought:
    """A fragment of model narration."""

    delta: str


@dataclass(frozen=True)
class Exhausted:
    """A research cell stopped because its budget ran out."""


AgentEvent = Query | Source | Thought | Exhausted

Sink = Callable[[AgentEvent, "str | None"], None]
"""Called as `sink(event, sub_question)`. Must be synchronous: it runs inside the
agent's event stream."""


def frame(event: AgentEvent, sub_question: str | None) -> dict[str, Any]:
    """One SSE frame: `{type, sub_question, payload}`."""
    match event:
        case Query(tool=tool, text=text):
            kind, payload = "query", {"tool": tool, "q": text, "hits": None}
        case Source(ref=ref):
            domain = urlparse(ref.url).netloc
            kind, payload = "source", {
                "url": ref.url,
                "domain": domain,
                "title": ref.title or domain or ref.url,
                "published_date": (
                    ref.published_date.isoformat() if ref.published_date else None
                ),
                # Nothing scores a domain, so nothing is invented.
                "credibility": None,
            }
        case Thought(delta=delta):
            kind, payload = "thought", {"delta": delta}
        case Exhausted():
            kind, payload = "exhausted", {}
        case _:
            raise TypeError(f"no frame for {type(event).__name__}")
    return {"type": kind, "sub_question": sub_question, "payload": payload}
