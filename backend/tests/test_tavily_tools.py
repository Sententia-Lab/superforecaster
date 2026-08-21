"""The two Tavily tools. No network: the SDK client is stubbed, so request bodies,
source recording, and the returned shape are asserted against the real code path."""

from __future__ import annotations

import pytest
from pydantic_ai import RunContext
from tavily.errors import InvalidAPIKeyError

from superforecaster.deps import ForecastDeps
from superforecaster.tools import tavily_tools

URL = "https://example.com/report"


@pytest.fixture
def tavily_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")


def make_ctx(deps: ForecastDeps) -> RunContext[ForecastDeps]:
    return RunContext(deps=deps, model=None, usage=None, prompt=None)


def stub(monkeypatch, payload: dict) -> dict:
    """Capture what the SDK was asked for, and answer without any network."""
    captured: dict = {}

    class StubClient:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

        async def _record(self, method, target, kw):
            captured["method"] = method
            captured["target"] = target
            captured.update(kw)
            return payload

        async def search(self, query, **kw):
            return await self._record("search", query, kw)

        async def extract(self, urls, **kw):
            return await self._record("extract", urls, kw)

    monkeypatch.setattr(tavily_tools, "AsyncTavilyClient", StubClient)
    return captured


def result(url: str) -> dict:
    return {"url": url, "title": "t", "content": "c", "raw_content": "whole page"}


# ---------- extract ----------


async def test_extract_records_a_source_per_page_and_returns_the_text(
    tavily_key, monkeypatch
):
    stub(
        monkeypatch,
        {
            "results": [
                {"url": URL, "title": "Q3 Report", "raw_content": "Revenue rose 4%."}
            ]
        },
    )
    deps = ForecastDeps()

    out = await tavily_tools.extract_pages(make_ctx(deps), [URL])

    assert out == [{"title": "Q3 Report", "url": URL, "text": "Revenue rose 4%."}]
    assert [(s.url, s.tool) for s in deps.sources_seen] == [(URL, "extract_pages")]


async def test_extract_caps_how_many_urls_reach_tavily(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": []})
    urls = [f"https://example.com/{i}" for i in range(10)]

    await tavily_tools.extract_pages(make_ctx(ForecastDeps()), urls)

    assert len(captured["target"]) == tavily_tools.MAX_EXTRACT_URLS


async def test_extract_truncates_long_pages(tavily_key, monkeypatch):
    stub(monkeypatch, {"results": [{"url": URL, "raw_content": "x" * 10_000}]})

    out = await tavily_tools.extract_pages(make_ctx(ForecastDeps()), [URL])

    assert len(out[0]["text"]) == tavily_tools.PAGE_CHARS


async def test_extract_with_no_urls_does_not_call_tavily(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": []})

    out = await tavily_tools.extract_pages(make_ctx(ForecastDeps()), [])

    assert "No URLs" in out
    assert "method" not in captured


# ---------- failure modes ----------


@pytest.mark.parametrize("call", ["search", "extract"])
async def test_no_key_is_not_an_error(call, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    ctx = make_ctx(ForecastDeps())
    out = (
        await tavily_tools.search_web(ctx, "q")
        if call == "search"
        else await tavily_tools.extract_pages(ctx, [URL])
    )
    assert "TAVILY_API_KEY" in out


@pytest.mark.parametrize("call", ["search", "extract"])
async def test_an_upstream_failure_is_not_an_exception(call, tavily_key, monkeypatch):
    class Boom:
        def __init__(self, api_key=None):
            pass

        async def search(self, *a, **k):
            raise InvalidAPIKeyError()

        async def extract(self, *a, **k):
            raise InvalidAPIKeyError()

    monkeypatch.setattr(tavily_tools, "AsyncTavilyClient", Boom)
    ctx = make_ctx(ForecastDeps())
    out = (
        await tavily_tools.search_web(ctx, "q")
        if call == "search"
        else await tavily_tools.extract_pages(ctx, [URL])
    )
    assert "error" in out.lower()


async def test_the_key_is_the_clients_not_the_calls(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": []})
    await tavily_tools.search_web(make_ctx(ForecastDeps()), "q")
    assert captured["api_key"] == "tvly-test"


# ---------- search ----------


async def test_search_web_clamps_what_one_call_may_spend(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": []})

    await tavily_tools.search_web(make_ctx(ForecastDeps()), "q")

    assert captured["max_results"] == tavily_tools.MAX_RESULTS
    assert captured["chunks_per_source"] == tavily_tools.MAX_CHUNKS_PER_SOURCE
    assert captured["search_depth"] == tavily_tools.SEARCH_DEPTH


async def test_the_agent_picks_the_topic(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": []})
    await tavily_tools.search_web(make_ctx(ForecastDeps()), "q", topic="news")
    assert captured["topic"] == "news"


async def test_search_web_returns_title_url_and_excerpt_only(tavily_key, monkeypatch):
    """`raw_content` is the whole page; `extract_pages` is the tool for that."""
    stub(monkeypatch, {"results": [result(URL)]})

    out = await tavily_tools.search_web(make_ctx(ForecastDeps()), "q")

    assert out == [{"title": "t", "url": URL, "content": "c"}]


async def test_search_web_records_every_result_as_a_source(tavily_key, monkeypatch):
    stub(monkeypatch, {"results": [result("https://a"), result("https://b")]})
    deps = ForecastDeps()

    await tavily_tools.search_web(make_ctx(deps), "steel tariffs")

    assert [s.url for s in deps.sources_seen] == ["https://a", "https://b"]
    assert {s.query for s in deps.sources_seen} == {"steel tariffs"}


async def test_no_results_is_a_sentence(tavily_key, monkeypatch):
    stub(monkeypatch, {"results": []})
    out = await tavily_tools.search_web(make_ctx(ForecastDeps()), "nothing")
    assert out == "No web results for: nothing"


async def test_a_quoted_phrase_turns_on_exact_match(monkeypatch, tavily_key):
    captured = stub(monkeypatch, {"results": []})
    await tavily_tools.search_web(make_ctx(ForecastDeps()), '"John Smith" CEO')
    assert captured["exact_match"] is True


async def test_an_unquoted_query_leaves_exact_match_off(monkeypatch, tavily_key):
    captured = stub(monkeypatch, {"results": []})
    await tavily_tools.search_web(make_ctx(ForecastDeps()), "John Smith CEO")
    assert captured["exact_match"] is False
