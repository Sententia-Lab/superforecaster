"""Agent events as the frames the browser reads.

`superforecaster.events` says what happened. This says how to put it on the wire, which
is the application's concern and not the library's: the field names below are a contract
with the frontend, and changing one of them breaks a rendering, not a forecast.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from superforecaster.events import AgentEvent, Exhausted, Query, Source, Thought


def frame(event: AgentEvent, sub_question: str | None) -> dict[str, Any]:
    """One SSE frame: `{type, sub_question, payload}`."""
    kind, payload = _payload(event)
    return {"type": kind, "sub_question": sub_question, "payload": payload}


def _payload(event: AgentEvent) -> tuple[str, dict[str, Any]]:
    match event:
        case Query(tool=tool, text=text):
            return "query", {"tool": tool, "q": text, "hits": None}
        case Source(ref=ref):
            return "source", _source_payload(ref)
        case Thought(delta=delta):
            return "thought", {"delta": delta}
        case Exhausted():
            return "exhausted", {}
    raise TypeError(f"no frame for {type(event).__name__}")


def _source_payload(ref: Any) -> dict[str, Any]:
    """A `SourceRef` as the UI's `source` event.

    `credibility` is None: nothing in the backend scores a domain today, and inventing
    a number to fill a coloured dot would be the UI lying with the server's authority.
    """
    url = getattr(ref, "url", "") or ""
    published = getattr(ref, "published_date", None)
    domain = urlparse(url).netloc
    return {
        "url": url,
        "domain": domain,
        # Falls back to the domain, then to the raw string. `SourceRef.title` used not
        # to exist, so this always reached the last branch — which printed an
        # unparseable URL in full as though it were a headline.
        "title": getattr(ref, "title", "") or domain or url,
        "query": getattr(ref, "query", ""),
        "published_date": published.isoformat() if published else None,
        "tool": getattr(ref, "tool", ""),
        "credibility": None,
    }
