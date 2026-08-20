"""Date parsing shared by every tool.

Its own module because both Tavily and Wikipedia hand back timestamps in more than one
shape: ISO 8601 in most Tavily responses, RFC 2822 in some, and a third form on a
MediaWiki revision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_published(raw: str | None) -> datetime | None:
    """Parse whatever the upstream put in a date field.

    It is ISO 8601 in most responses and RFC 2822 in some, so both are tried.
    Returns None when the value is missing or unparseable — the caller decides what
    an unknown date means.
    """
    if not raw:
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return _as_utc(parsedate_to_datetime(raw))
    except (TypeError, ValueError):
        return None
