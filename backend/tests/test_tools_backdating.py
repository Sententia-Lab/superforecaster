"""Tests for clamp 1 — the tools may not return anything published after `as_of`.

These earn their place because a bug here is silent. If `end_date` quietly stops
reaching Tavily, every backtest still runs, still prints a scorecard, and still looks
green — while the agent reads the answer off a 2024 news article about a 2022
question. Nothing at runtime would flag it.

No network: the pure request builders are asserted directly, and the two tools that
do I/O are driven through a stubbed httpx client.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic_ai import RunContext

from superforecaster.deps import ForecastDeps
from superforecaster.tools import dates, tavily_tools, wikipedia_tools

AS_OF = datetime(2022, 2, 1, tzinfo=timezone.utc)


# ---------- _search_kwargs ----------


def test_search_kwargs_carries_end_date_when_clamped():
    assert tavily_tools._search_kwargs(AS_OF)["end_date"] == "2022-02-01"


def test_search_kwargs_switches_to_news_topic_when_clamped():
    """Tavily only returns published_date on news results, and _drop_leaked needs it."""
    assert tavily_tools._search_kwargs(AS_OF)["topic"] == "news"


def test_search_kwargs_is_empty_when_unclamped():
    """No date arguments at all on a live run, so Tavily applies no filter."""
    assert tavily_tools._search_kwargs(None) == {}


# ---------- _wikipedia_params ----------


def test_wikipedia_params_requests_a_historical_revision_when_clamped():
    params = wikipedia_tools._wikipedia_params("Ukraine", AS_OF)
    assert params["prop"] == "revisions"
    assert params["rvstart"] == "2022-02-01T00:00:00Z"
    assert params["rvdir"] == "older"
    assert params["rvlimit"] == 1


def test_wikipedia_params_requests_the_current_article_when_unclamped():
    params = wikipedia_tools._wikipedia_params("Ukraine", None)
    assert params["prop"] == "extracts"
    assert "rvstart" not in params


# ---------- _parse_published ----------


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


# ---------- _drop_leaked ----------


def result(url: str, published: str | None) -> dict:
    return {"url": url, "title": "t", "content": "c", "published_date": published}


def test_drop_leaked_keeps_results_published_before_as_of():
    kept, refs = tavily_tools._drop_leaked([result("a", "2022-01-15T00:00:00Z")], AS_OF)
    assert [r["url"] for r in kept] == ["a"]
    assert refs[0].is_leak is False


def test_drop_leaked_removes_results_published_after_as_of():
    """The whole point: a 2024 article must never reach a 2022 question."""
    kept, refs = tavily_tools._drop_leaked(
        [result("late", "2024-03-01T00:00:00Z")], AS_OF
    )
    assert kept == []
    assert len(refs) == 1
    assert refs[0].is_leak is True


def test_drop_leaked_drops_undated_results_when_clamped():
    """An undated article cannot be shown to predate the question, so it goes."""
    kept, _ = tavily_tools._drop_leaked([result("undated", None)], AS_OF)
    assert kept == []


def test_drop_leaked_records_every_result_it_considered():
    """Dropped results still appear in the audit trail, or filtering is invisible."""
    raw = [
        result("keep", "2022-01-01T00:00:00Z"),
        result("drop", "2023-01-01T00:00:00Z"),
    ]
    kept, refs = tavily_tools._drop_leaked(raw, AS_OF)
    assert len(kept) == 1
    assert {r.url for r in refs} == {"keep", "drop"}


def test_drop_leaked_is_a_passthrough_when_unclamped():
    """Production keeps undated results — the clamp only applies to backtests."""
    raw = [result("a", None), result("b", "2026-01-01T00:00:00Z")]
    kept, refs = tavily_tools._drop_leaked(raw, None)
    assert kept == raw
    assert all(r.is_leak is False for r in refs)


# ---------- search_web through a stubbed transport ----------


def make_ctx(deps: ForecastDeps) -> RunContext[ForecastDeps]:
    return RunContext(deps=deps, model=None, usage=None, prompt=None)


@pytest.fixture
def tavily_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")


def stub_tavily(monkeypatch, results: list[dict]) -> dict:
    """Capture the search arguments and return canned results without any network."""
    captured: dict = {}

    class StubClient:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key

        async def search(self, query, **kw):
            captured["query"] = query
            captured.update(kw)
            return {"results": results}

    monkeypatch.setattr(tavily_tools, "AsyncTavilyClient", StubClient)
    return captured


async def test_search_web_sends_end_date_to_tavily(monkeypatch, tavily_key):
    captured = stub_tavily(monkeypatch, [result("a", "2022-01-05T00:00:00Z")])
    ctx = make_ctx(ForecastDeps(as_of=AS_OF))

    await tavily_tools.search_web(ctx, "russia ukraine")

    assert captured["end_date"] == "2022-02-01"
    # The key is the client's, not the call's, so `_search_kwargs` stays free of secrets
    # and can be asserted on directly.
    assert captured["api_key"] == "tvly-test"
    assert "api_key" not in tavily_tools._search_kwargs(AS_OF)


async def test_search_web_withholds_leaked_results(monkeypatch, tavily_key):
    stub_tavily(monkeypatch, [result("late", "2024-06-01T00:00:00Z")])
    ctx = make_ctx(ForecastDeps(as_of=AS_OF))

    out = await tavily_tools.search_web(ctx, "q")

    assert "withheld" in out
    assert "late" not in out


async def test_search_web_records_sources_on_deps(monkeypatch, tavily_key):
    stub_tavily(monkeypatch, [result("a", "2022-01-05T00:00:00Z")])
    deps = ForecastDeps(as_of=AS_OF)

    await tavily_tools.search_web(make_ctx(deps), "q")

    assert [s.url for s in deps.sources_seen] == ["a"]
    assert deps.leaked_sources == []


async def test_leaked_sources_surfaces_a_clamp_failure(monkeypatch, tavily_key):
    """If filtering ever regresses, this is the property that catches it."""
    stub_tavily(monkeypatch, [result("late", "2024-06-01T00:00:00Z")])
    deps = ForecastDeps(as_of=AS_OF)

    await tavily_tools.search_web(make_ctx(deps), "q")

    assert [s.url for s in deps.leaked_sources] == ["late"]


async def test_search_web_passes_the_models_arguments_through(monkeypatch, tavily_key):
    captured = stub_tavily(monkeypatch, [result("a", "2022-01-05T00:00:00Z")])

    await tavily_tools.search_web(
        make_ctx(ForecastDeps()),
        "q",
        search_depth="advanced",
        include_domains=["ons.gov.uk"],
        include_answer="basic",
        days=7,
    )

    assert captured["search_depth"] == "advanced"
    assert captured["include_domains"] == ["ons.gov.uk"]
    assert captured["include_answer"] == "basic"
    assert captured["days"] == 7
    assert "topic" not in captured, "an argument the model omitted must not be invented"


async def test_search_web_clamps_what_one_call_may_spend(monkeypatch, tavily_key):
    captured = stub_tavily(monkeypatch, [result("a", "2022-01-05T00:00:00Z")])

    await tavily_tools.search_web(
        make_ctx(ForecastDeps()), "q", max_results=50, chunks_per_source=99
    )

    assert captured["max_results"] == tavily_tools.MAX_RESULTS
    assert captured["chunks_per_source"] == tavily_tools.MAX_CHUNKS_PER_SOURCE
    assert (
        captured["timeout"] == tavily_tools._TIMEOUT
    ), "timeout is not the model's to set"


async def test_backtest_clamp_beats_the_models_dates(monkeypatch, tavily_key):
    """The model may now pass dates. It must not be able to widen the clamp with them."""
    captured = stub_tavily(monkeypatch, [result("a", "2022-01-05T00:00:00Z")])

    await tavily_tools.search_web(
        make_ctx(ForecastDeps(as_of=AS_OF)),
        "q",
        topic="general",
        start_date="2026-01-01",
        end_date="2026-08-01",
    )

    assert captured["end_date"] == "2022-02-01"
    assert captured["start_date"] < "2022-02-01"
    assert captured["topic"] == "news"


async def test_search_web_without_key_is_not_an_error(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = await tavily_tools.search_web(make_ctx(ForecastDeps()), "q")
    assert "unavailable" in out


# ---------- _extract_page_text ----------


def test_extract_page_text_reads_the_current_extract_when_unclamped():
    page = {"extract": "current intro text"}
    text, revision_date = wikipedia_tools._extract_page_text(page, None)
    assert text == "current intro text"
    assert revision_date is None


def test_extract_page_text_reads_the_historical_revision_when_clamped():
    """Revisions arrive under revisions[0].slots.main, not under `extract`."""
    page = {
        "revisions": [
            {
                "timestamp": "2022-01-20T10:00:00Z",
                "slots": {"main": {"*": "text as of January 2022"}},
            }
        ]
    }
    text, revision_date = wikipedia_tools._extract_page_text(page, AS_OF)
    assert text == "text as of January 2022"
    assert revision_date is not None
    assert revision_date.date().isoformat() == "2022-01-20"


def test_extract_page_text_is_empty_when_the_article_did_not_exist_yet():
    text, revision_date = wikipedia_tools._extract_page_text({"revisions": []}, AS_OF)
    assert text == ""
    assert revision_date is None


def test_extract_page_text_falls_back_to_the_unslotted_shape():
    """Older MediaWiki responses put content directly on the revision."""
    page = {"revisions": [{"timestamp": "2022-01-20T10:00:00Z", "*": "legacy shape"}]}
    text, _ = wikipedia_tools._extract_page_text(page, AS_OF)
    assert text == "legacy shape"


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


def test_search_kwargs_always_sends_a_start_date_with_the_end_date():
    """The one that looks redundant and is not.

    Tavily silently ignores `end_date` when it arrives without `start_date` — measured at
    15 of 15 results published after the cutoff, versus 0 of 15 once `start_date` was added.
    Nothing errors, so the only thing that would catch a regression is this assertion.
    """
    kwargs = tavily_tools._search_kwargs(AS_OF)

    assert kwargs["end_date"] == "2022-02-01"
    assert kwargs["start_date"] < kwargs["end_date"]
    assert kwargs["topic"] == "news"
