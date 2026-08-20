"""Timestamp parsing, shared by both upstreams.

Tavily returns ISO 8601 in most responses and RFC 2822 in some, and MediaWiki stamps
revisions in a third shape. One parse serves all three, so it is tested once here.
"""

from __future__ import annotations

import pytest

from superforecaster.tools import dates


@pytest.mark.parametrize(
    "raw",
    [
        "2022-01-15T00:00:00Z",
        "2022-01-15T00:00:00+00:00",
        "Sat, 15 Jan 2022 00:00:00 GMT",
    ],
)
def test_parse_published_accepts_both_formats(raw):
    """Tavily returns ISO 8601 in most responses and RFC 2822 in some."""
    parsed = dates._parse_published(raw)
    assert parsed is not None
    assert parsed.year == 2022 and parsed.month == 1 and parsed.day == 15


@pytest.mark.parametrize("raw", [None, "", "not a date"])
def test_parse_published_returns_none_on_junk(raw):
    assert dates._parse_published(raw) is None
