"""Tests for the Wikipedia tool's request headers.

The property that earns this file: **every request carries a `User-Agent`.** Wikimedia
answers a default client agent with 403, and the tool turns that into a readable string
rather than an exception — so a missing header costs an agent one of its eight tool calls
and looks like an ordinary empty result. Nothing else would catch it.

No network: `httpx.AsyncClient` is stubbed, so the headers are asserted against the real
code path.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic_ai import RunContext

from superforecaster.deps import ForecastDeps
from superforecaster.tools import wikipedia_tools


def make_ctx(deps: ForecastDeps) -> RunContext[ForecastDeps]:
    return RunContext(deps=deps, model=None, usage=None, prompt=None)


@pytest.fixture
def no_wikipedia_key(monkeypatch):
    monkeypatch.delenv("WIKIPEDIA_API_KEY", raising=False)


# ---------- the headers themselves ----------


def test_a_user_agent_is_sent_without_a_key(no_wikipedia_key):
    """The case that was broken. Wikimedia needs no key here, but it does need a name."""
    headers = wikipedia_tools._wikipedia_headers()

    assert headers["User-Agent"] == wikipedia_tools.USER_AGENT
    assert "Authorization" not in headers


def test_a_user_agent_is_sent_with_a_key(monkeypatch):
    monkeypatch.setenv("WIKIPEDIA_API_KEY", "wiki-abc")
    headers = wikipedia_tools._wikipedia_headers()

    assert headers["User-Agent"] == wikipedia_tools.USER_AGENT
    assert headers["Authorization"] == "Bearer wiki-abc"


def test_the_user_agent_names_the_project_and_a_contact_url():
    """Wikimedia's policy asks for both. A bare version string is refused."""
    assert "superforecaster" in wikipedia_tools.USER_AGENT
    assert "https://" in wikipedia_tools.USER_AGENT


# ---------- the header reaches the request ----------


def stub(monkeypatch, payload: dict) -> list[httpx.Headers]:
    """Capture the headers of every request, and answer without any network."""
    seen: list[httpx.Headers] = []

    class StubClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            seen.append(httpx.Headers(headers or {}))
            # `raise_for_status` needs the request attached, even on a 200.
            return httpx.Response(
                200, json=payload, request=httpx.Request("GET", url, params=params)
            )

    monkeypatch.setattr(wikipedia_tools.httpx, "AsyncClient", StubClient)
    return seen


async def test_search_wikipedia_sends_the_user_agent_on_every_request(
    monkeypatch, no_wikipedia_key
):
    """Both requests, not just the first — the article fetch is a separate call."""
    seen = stub(
        monkeypatch,
        {
            "query": {
                "search": [{"title": "Alphabet Inc."}],
                "pages": {"1": {"extract": "Alphabet Inc. is a holding company."}},
            }
        },
    )

    out = await wikipedia_tools.search_wikipedia(make_ctx(ForecastDeps()), "Alphabet")

    assert len(seen) == 2, "a search request and an article request"
    for headers in seen:
        assert headers["user-agent"] == wikipedia_tools.USER_AGENT
    assert "Alphabet Inc." in out


async def test_a_403_is_reported_rather_than_raised(monkeypatch, no_wikipedia_key):
    """The contract every tool shares: a dead upstream is missing information, not a crash.

    This is also what hid the missing header — the 403 read as an ordinary empty result.
    """

    class Refusing:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                "403 Forbidden", request=request, response=httpx.Response(403)
            )

    monkeypatch.setattr(wikipedia_tools.httpx, "AsyncClient", Refusing)

    out = await wikipedia_tools.search_wikipedia(make_ctx(ForecastDeps()), "Alphabet")

    assert "Wikipedia error" in out
