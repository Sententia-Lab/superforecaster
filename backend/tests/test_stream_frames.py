"""The SSE frame shapes, pinned against what the browser actually reads.

`app/stream.py` is the only place the wire format is written down now that core emits
typed events instead of dicts. The field names below are read by
`frontend/src/hooks/useStepStream.js` — renaming one here breaks a rendering silently,
which is exactly the failure a refactor of the tracing layer could have introduced.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app import stream
from superforecaster.events import Exhausted, Query, Source, Thought
from superforecaster.models import SourceRef


def test_thought_carries_the_delta_the_ui_appends():
    frame = stream.frame(Thought(delta="think"), "sq1")

    assert frame == {
        "type": "thought",
        "sub_question": "sq1",
        "payload": {"delta": "think"},
    }


def test_query_carries_the_tool_and_text_the_ui_concatenates():
    frame = stream.frame(Query(tool="search_web", text="uk cpi"), None)

    assert frame["type"] == "query"
    assert frame["payload"] == {"tool": "search_web", "q": "uk cpi", "hits": None}


def test_source_derives_the_domain_and_never_invents_credibility():
    ref = SourceRef(
        url="https://www.ons.gov.uk/economy/bulletin",
        title="CPI bulletin",
        query="uk cpi",
        tool="search_web",
        published_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    payload = stream.frame(Source(ref=ref), "sq2")["payload"]

    assert payload["domain"] == "www.ons.gov.uk"
    assert payload["title"] == "CPI bulletin"
    assert payload["published_date"] == "2026-03-01T00:00:00+00:00"
    assert payload["credibility"] is None


def test_source_title_falls_back_to_the_domain_not_the_raw_url():
    """An untitled source used to print an unparseable URL as though it were a headline."""
    ref = SourceRef(url="https://example.com/a/b/c", query="q", tool="search_web")
    payload = stream.frame(Source(ref=ref), None)["payload"]

    assert payload["title"] == "example.com"


def test_exhausted_is_a_bare_signal():
    """The UI reads only the type — it swaps the query line for a fixed message."""
    frame = stream.frame(Exhausted(), "sq3")

    assert frame["type"] == "exhausted"
    assert frame["sub_question"] == "sq3"
