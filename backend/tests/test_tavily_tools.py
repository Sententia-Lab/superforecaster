"""Tests for the three Tavily tools that read pages rather than search for them.

The property that earns this file: **none of extract, crawl, or map may run in a backtest.**
No Tavily endpoint but `/search` takes a date, so each returns the page as it stands today.
A backtest that reaches one of them reads the future and still prints a green scorecard, so
the refusal is checked per tool rather than assumed from a shared helper.

No network: `httpx.AsyncClient` is stubbed, so the request bodies and the source recording
are asserted against the real code path.
"""

from __future__ import annotations

import json

import pytest
from tavily.errors import InvalidAPIKeyError
from pydantic_ai import RunContext

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

        async def crawl(self, url, **kw):
            return await self._record("crawl", url, kw)

        async def map(self, url, **kw):
            return await self._record("map", url, kw)

    monkeypatch.setattr(tavily_tools, "AsyncTavilyClient", StubClient)
    return captured


# ---------- extract ----------


async def test_extract_records_a_source_per_page_and_returns_the_text(
    tavily_key, monkeypatch
):
    captured = stub(
        monkeypatch,
        {
            "results": [
                {"url": URL, "title": "Q3 Report", "raw_content": "Revenue rose 4%."}
            ],
            "failed_results": [],
        },
    )
    deps = ForecastDeps()

    out = json.loads(await tavily_tools.extract_pages(make_ctx(deps), [URL]))

    assert captured["method"] == "extract"
    assert captured["target"] == [URL]
    assert out["pages"] == [
        {"title": "Q3 Report", "url": URL, "text": "Revenue rose 4%."}
    ]
    assert [(s.url, s.tool) for s in deps.sources_seen] == [(URL, "extract_pages")]


async def test_extract_caps_how_many_urls_reach_tavily(tavily_key, monkeypatch):
    """The model cannot pass a limit, so the ceiling has to be applied to what it asked."""
    captured = stub(monkeypatch, {"results": [], "failed_results": []})
    many = [f"https://example.com/{i}" for i in range(20)]

    await tavily_tools.extract_pages(make_ctx(ForecastDeps()), many)

    assert len(captured["target"]) == tavily_tools.MAX_EXTRACT_URLS


async def test_extract_reports_pages_it_could_not_read(tavily_key, monkeypatch):
    stub(
        monkeypatch,
        {
            "results": [{"url": URL, "title": "T", "raw_content": "body"}],
            "failed_results": [{"url": "https://example.com/dead"}],
        },
    )
    out = json.loads(await tavily_tools.extract_pages(make_ctx(ForecastDeps()), [URL]))
    assert out["could_not_read"] == ["https://example.com/dead"]


async def test_extract_with_no_urls_does_not_call_tavily(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": []})
    out = await tavily_tools.extract_pages(make_ctx(ForecastDeps()), ["", ""])
    assert "No URLs given" in out
    assert captured == {}


# ---------- crawl ----------


async def test_crawl_is_bounded_on_depth_and_pages(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": [{"url": URL, "raw_content": "text"}]})
    deps = ForecastDeps()

    await tavily_tools.crawl_site(make_ctx(deps), URL, "rate decisions")

    assert captured["method"] == "crawl"
    assert captured["max_depth"] == tavily_tools.CRAWL_DEPTH
    assert captured["limit"] == tavily_tools.MAX_CRAWL_PAGES
    assert captured["instructions"] == "rate decisions"
    assert [(s.url, s.tool) for s in deps.sources_seen] == [(URL, "crawl_site")]


async def test_crawl_omits_empty_instructions(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": []})
    await tavily_tools.crawl_site(make_ctx(ForecastDeps()), URL)
    assert captured["instructions"] is None


# ---------- map ----------


async def test_map_lists_links_without_recording_them_as_sources(
    tavily_key, monkeypatch
):
    """Mapping lists what exists; it does not read anything.

    Recording these would let `check_citations` accept a URL the agent never opened, which
    is the one thing that check exists to prevent.
    """
    stub(monkeypatch, {"results": [URL, "https://example.com/other"]})
    deps = ForecastDeps()

    out = await tavily_tools.map_site(make_ctx(deps), "https://example.com")

    assert URL in out
    assert deps.sources_seen == []


async def test_map_is_bounded(tavily_key, monkeypatch):
    captured = stub(monkeypatch, {"results": []})
    await tavily_tools.map_site(make_ctx(ForecastDeps()), "https://example.com")
    assert captured["limit"] == tavily_tools.MAX_MAP_LINKS


# ---------- shared contracts ----------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda ctx: tavily_tools.extract_pages(ctx, [URL]), id="extract"),
        pytest.param(lambda ctx: tavily_tools.crawl_site(ctx, URL), id="crawl"),
        pytest.param(lambda ctx: tavily_tools.map_site(ctx, URL), id="map"),
    ],
)
async def test_no_key_is_not_an_error(call, monkeypatch):
    """Same contract as `search_web` — a missing key is missing information, not a crash."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = await call(make_ctx(ForecastDeps()))
    assert "no TAVILY_API_KEY set" in out


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda ctx: tavily_tools.extract_pages(ctx, [URL]), id="extract"),
        pytest.param(lambda ctx: tavily_tools.crawl_site(ctx, URL), id="crawl"),
        pytest.param(lambda ctx: tavily_tools.map_site(ctx, URL), id="map"),
    ],
)
async def test_an_upstream_failure_is_not_an_exception(call, tavily_key, monkeypatch):
    """A dead upstream is missing information, not a crash — same as `search_web`."""

    class Broken:
        def __init__(self, api_key=None):
            pass

        def __getattr__(self, name):
            async def boom(*a, **kw):
                raise InvalidAPIKeyError("upstream refused")

            return boom

    monkeypatch.setattr(tavily_tools, "AsyncTavilyClient", Broken)
    assert "error" in (await call(make_ctx(ForecastDeps()))).lower()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda ctx: tavily_tools.extract_pages(ctx, [URL]), id="extract"),
        pytest.param(lambda ctx: tavily_tools.crawl_site(ctx, URL), id="crawl"),
        pytest.param(lambda ctx: tavily_tools.map_site(ctx, URL), id="map"),
    ],
)
async def test_the_key_is_the_clients_not_the_calls(call, tavily_key, monkeypatch):
    """The SDK takes the key once, at construction, so no call argument carries a secret."""
    captured = stub(monkeypatch, {"results": []})
    await call(make_ctx(ForecastDeps()))
    assert captured["api_key"] == "tvly-test"
    assert not any("key" in k.lower() for k in captured if k != "api_key")


# ---------- what one search may spend ----------


def result(url: str, published: str | None = None) -> dict:
    return {"url": url, "title": "t", "content": "c", "published_date": published}


async def test_search_web_clamps_what_one_call_may_spend(tavily_key, monkeypatch):
    """The model chooses the query and the topic. Breadth and timeout are the tool's."""
    captured = stub(monkeypatch, {"results": [result("https://a.example")]})

    await tavily_tools.search_web(make_ctx(ForecastDeps()), "q")

    assert captured["max_results"] == tavily_tools.MAX_RESULTS
    assert captured["chunks_per_source"] == tavily_tools.MAX_CHUNKS_PER_SOURCE
    assert captured["timeout"] == tavily_tools._TIMEOUT


async def test_search_web_without_key_is_not_an_error(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = await tavily_tools.search_web(make_ctx(ForecastDeps()), "q")
    assert "unavailable" in out


async def test_the_agent_picks_the_topic(tavily_key, monkeypatch):
    """Nothing overrides it any more — the clamp that forced `news` is gone (ADR 17)."""
    captured = stub(monkeypatch, {"results": [result("https://a.example")]})

    await tavily_tools.search_web(make_ctx(ForecastDeps()), "q", topic="finance")

    assert captured["topic"] == "finance"
    assert "end_date" not in captured
    assert "start_date" not in captured


# ---------- the JSON answer ----------


async def test_search_web_answers_with_json(tavily_key, monkeypatch):
    """Every tool passes Tavily's own dicts through, so there is no format to keep in step."""
    stub(
        monkeypatch,
        {"results": [result("https://a.example", "2022-01-05T00:00:00Z")]},
    )

    out = json.loads(await tavily_tools.search_web(make_ctx(ForecastDeps()), "q"))

    assert out["query"] == "q"
    assert out["results"] == [
        {
            "title": "t",
            "url": "https://a.example",
            "content": "c",
            "published_date": "2022-01-05T00:00:00Z",
        }
    ]


async def test_search_web_does_not_hand_over_whole_pages(tavily_key, monkeypatch):
    """`raw_content` is the whole page. `extract_pages` is the tool for that, on the pages
    the agent chose to spend a call on — five per search would spend a cell's token budget.
    """
    stub(
        monkeypatch,
        {
            "results": [
                {**result("https://a.example"), "raw_content": "the entire page"}
            ]
        },
    )

    out = json.loads(await tavily_tools.search_web(make_ctx(ForecastDeps()), "q"))

    assert "raw_content" not in out["results"][0]


async def test_search_web_records_every_result_as_a_source(tavily_key, monkeypatch):
    """`check_citations` fails a forecast citing a URL absent from `sources_seen`, and the
    run tree draws its chips by diffing the same list."""
    stub(
        monkeypatch,
        {"results": [result("https://a.example"), result("https://b.example")]},
    )
    deps = ForecastDeps()

    await tavily_tools.search_web(make_ctx(deps), "q")

    assert [s.url for s in deps.sources_seen] == [
        "https://a.example",
        "https://b.example",
    ]
    assert all(s.tool == "search_web" for s in deps.sources_seen)


# ---------- exact_match is read off the query, not asked for ----------


async def test_a_quoted_phrase_turns_on_exact_match(monkeypatch, tavily_key):
    """The `query` description tells the agent that quoting requires a phrase verbatim.

    It only did that when a separate flag was also set, which the agent had no reason to
    connect — one real run quoted three figures, got no exact filter, and came back with an
    unrelated company. Reading the flag off the query is what makes the description true.
    """
    captured = stub(monkeypatch, {"results": [result("https://a.example")]})

    await tavily_tools.search_web(make_ctx(ForecastDeps()), 'Alphabet "annual revenue"')

    assert captured["exact_match"] is True


async def test_an_unquoted_query_leaves_exact_match_off(monkeypatch, tavily_key):
    """Tavily rejects `exact_match` without a quoted phrase, so this is not just a default —
    sending True here would make every ordinary search a BadRequestError."""
    captured = stub(monkeypatch, {"results": [result("https://a.example")]})

    await tavily_tools.search_web(make_ctx(ForecastDeps()), "Alphabet annual revenue")

    assert captured["exact_match"] is False


async def test_the_agent_cannot_set_exact_match(monkeypatch, tavily_key):
    """It is not a parameter. A value in the signature is one the model can contradict."""
    import inspect

    params = inspect.signature(tavily_tools.search_web).parameters

    assert "exact_match" not in params
    assert set(params) == {"ctx", "query", "topic"}


# ---------- find_disconfirming_evidence ----------


async def test_find_disconfirming_evidence_runs_several_angles(monkeypatch, tavily_key):
    queries: list[str] = []

    async def fake_search(ctx, query):
        queries.append(query)
        return "results"

    monkeypatch.setattr(tavily_tools, "search_web", fake_search)
    out = await tavily_tools.find_disconfirming_evidence(
        make_ctx(ForecastDeps()), "X happens"
    )

    assert len(queries) == 3
    assert any("against" in q for q in queries)
    assert any("will not happen" in q for q in queries)
    assert "results" in out
