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

import httpx
import pytest
from pydantic_ai import RunContext

from superforecaster import tools
from superforecaster.deps import ForecastDeps, SearchBudget

AS_OF = datetime(2022, 2, 1, tzinfo=timezone.utc)


# ---------- _tavily_body ----------


def test_tavily_body_carries_end_date_when_clamped():
    body = tools._tavily_body("russia ukraine", AS_OF)
    assert body["end_date"] == "2022-02-01"


def test_tavily_body_switches_to_news_topic_when_clamped():
    """Tavily only returns published_date on news results, and _drop_leaked needs it."""
    assert tools._tavily_body("q", AS_OF)["topic"] == "news"


def test_tavily_body_omits_date_params_when_unclamped():
    body = tools._tavily_body("q", None)
    assert "end_date" not in body
    assert "topic" not in body


def test_tavily_body_carries_no_secret():
    """The api_key is merged in at call time so this dict is safe to assert on."""
    assert "api_key" not in tools._tavily_body("q", AS_OF)


# ---------- _wikipedia_params ----------


def test_wikipedia_params_requests_a_historical_revision_when_clamped():
    params = tools._wikipedia_params("Ukraine", AS_OF)
    assert params["prop"] == "revisions"
    assert params["rvstart"] == "2022-02-01T00:00:00Z"
    assert params["rvdir"] == "older"
    assert params["rvlimit"] == 1


def test_wikipedia_params_requests_the_current_article_when_unclamped():
    params = tools._wikipedia_params("Ukraine", None)
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
    parsed = tools._parse_published(raw)
    assert parsed is not None
    assert parsed.year == 2022 and parsed.month == 1 and parsed.day == 15


@pytest.mark.parametrize("raw", [None, "", "not a date"])
def test_parse_published_returns_none_on_junk(raw):
    assert tools._parse_published(raw) is None


# ---------- _drop_leaked ----------


def result(url: str, published: str | None) -> dict:
    return {"url": url, "title": "t", "content": "c", "published_date": published}


def test_drop_leaked_keeps_results_published_before_as_of():
    kept, refs = tools._drop_leaked([result("a", "2022-01-15T00:00:00Z")], AS_OF)
    assert [r["url"] for r in kept] == ["a"]
    assert refs[0].is_leak is False


def test_drop_leaked_removes_results_published_after_as_of():
    """The whole point: a 2024 article must never reach a 2022 question."""
    kept, refs = tools._drop_leaked([result("late", "2024-03-01T00:00:00Z")], AS_OF)
    assert kept == []
    assert len(refs) == 1
    assert refs[0].is_leak is True


def test_drop_leaked_drops_undated_results_when_clamped():
    """An undated article cannot be shown to predate the question, so it goes."""
    kept, _ = tools._drop_leaked([result("undated", None)], AS_OF)
    assert kept == []


def test_drop_leaked_records_every_result_it_considered():
    """Dropped results still appear in the audit trail, or filtering is invisible."""
    raw = [
        result("keep", "2022-01-01T00:00:00Z"),
        result("drop", "2023-01-01T00:00:00Z"),
    ]
    kept, refs = tools._drop_leaked(raw, AS_OF)
    assert len(kept) == 1
    assert {r.url for r in refs} == {"keep", "drop"}


def test_drop_leaked_is_a_passthrough_when_unclamped():
    """Production keeps undated results — the clamp only applies to backtests."""
    raw = [result("a", None), result("b", "2026-01-01T00:00:00Z")]
    kept, refs = tools._drop_leaked(raw, None)
    assert kept == raw
    assert all(r.is_leak is False for r in refs)


# ---------- search_web through a stubbed transport ----------


def make_ctx(deps: ForecastDeps) -> RunContext[ForecastDeps]:
    return RunContext(deps=deps, model=None, usage=None, prompt=None)


@pytest.fixture
def tavily_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")


def stub_tavily(monkeypatch, results: list[dict]) -> dict:
    """Capture the request body and return canned results without any network."""
    captured: dict = {}

    class StubClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(
                200, json={"results": results}, request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(tools.httpx, "AsyncClient", StubClient)
    return captured


async def test_search_web_sends_end_date_to_tavily(monkeypatch, tavily_key):
    captured = stub_tavily(monkeypatch, [result("a", "2022-01-05T00:00:00Z")])
    ctx = make_ctx(ForecastDeps(as_of=AS_OF))

    await tools.search_web(ctx, "russia ukraine")

    assert captured["json"]["end_date"] == "2022-02-01"
    assert captured["json"]["api_key"] == "tvly-test"


async def test_search_web_withholds_leaked_results(monkeypatch, tavily_key):
    stub_tavily(monkeypatch, [result("late", "2024-06-01T00:00:00Z")])
    ctx = make_ctx(ForecastDeps(as_of=AS_OF))

    out = await tools.search_web(ctx, "q")

    assert "withheld" in out
    assert "late" not in out


async def test_search_web_records_sources_on_deps(monkeypatch, tavily_key):
    stub_tavily(monkeypatch, [result("a", "2022-01-05T00:00:00Z")])
    deps = ForecastDeps(as_of=AS_OF)

    await tools.search_web(make_ctx(deps), "q")

    assert [s.url for s in deps.sources_seen] == ["a"]
    assert deps.leaked_sources == []


async def test_leaked_sources_surfaces_a_clamp_failure(monkeypatch, tavily_key):
    """If filtering ever regresses, this is the property that catches it."""
    stub_tavily(monkeypatch, [result("late", "2024-06-01T00:00:00Z")])
    deps = ForecastDeps(as_of=AS_OF)

    await tools.search_web(make_ctx(deps), "q")

    assert [s.url for s in deps.leaked_sources] == ["late"]


async def test_search_web_without_key_is_not_an_error(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = await tools.search_web(make_ctx(ForecastDeps()), "q")
    assert "unavailable" in out


# ---------- _extract_page_text ----------


def test_extract_page_text_reads_the_current_extract_when_unclamped():
    page = {"extract": "current intro text"}
    text, revision_date = tools._extract_page_text(page, None)
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
    text, revision_date = tools._extract_page_text(page, AS_OF)
    assert text == "text as of January 2022"
    assert revision_date is not None
    assert revision_date.date().isoformat() == "2022-01-20"


def test_extract_page_text_is_empty_when_the_article_did_not_exist_yet():
    text, revision_date = tools._extract_page_text({"revisions": []}, AS_OF)
    assert text == ""
    assert revision_date is None


def test_extract_page_text_falls_back_to_the_unslotted_shape():
    """Older MediaWiki responses put content directly on the revision."""
    page = {"revisions": [{"timestamp": "2022-01-20T10:00:00Z", "*": "legacy shape"}]}
    text, _ = tools._extract_page_text(page, AS_OF)
    assert text == "legacy shape"


# ---------- find_disconfirming_evidence ----------


async def test_find_disconfirming_evidence_runs_several_angles(monkeypatch, tavily_key):
    queries: list[str] = []

    async def fake_search(ctx, query):
        queries.append(query)
        return "results"

    # `_search_web`, not the tool: the sweep deliberately bypasses the tool wrapper so
    # its three searches cost one call against the budget, matching the one decision the
    # model actually made.
    monkeypatch.setattr(tools, "_search_web", fake_search)
    out = await tools.find_disconfirming_evidence(make_ctx(ForecastDeps()), "X happens")

    assert len(queries) == 3
    assert any("against" in q for q in queries)
    assert any("will not happen" in q for q in queries)
    assert "results" in out


async def test_a_disconfirming_sweep_costs_one_call_not_three(monkeypatch, tavily_key):
    """`SearchBudget.used` and `UsageLimits.tool_calls_limit` have to count the same
    thing, or the cline fires at a depth the wall does not agree with."""

    async def fake_search(ctx, query):
        return "results"

    monkeypatch.setattr(tools, "_search_web", fake_search)
    deps = ForecastDeps(budget=SearchBudget(sub_claim="sc1", soft_depth=5, hard_depth=8))
    await tools.find_disconfirming_evidence(make_ctx(deps), "X happens")

    assert deps.budget.used == 1


# ---------- the search budget ----------


def budgeted(soft: int = 3, hard: int = 5, used: int = 0) -> ForecastDeps:
    return ForecastDeps(
        budget=SearchBudget(sub_claim="sc1", soft_depth=soft, hard_depth=hard, used=used)
    )


def test_below_the_cline_the_notice_is_just_a_count():
    deps = budgeted(used=0)
    notice = tools._budget_notice(make_ctx(deps))

    assert "1 of 3 used" in notice
    assert "Stop searching" not in notice


def test_past_the_cline_the_notice_tells_the_agent_to_converge():
    deps = budgeted(used=2)  # this call makes it 3, which is the cline
    notice = tools._budget_notice(make_ctx(deps))

    assert "SEARCH BUDGET SPENT" in notice
    assert "Stop searching" in notice
    assert "2 calls remain" in notice


def test_at_the_wall_the_notice_says_it_is_the_last_result():
    deps = budgeted(used=4)  # this call makes it 5 == hard_depth
    notice = tools._budget_notice(make_ctx(deps))

    assert "EXHAUSTED" in notice
    assert "last tool result" in notice
    assert deps.budget.exhausted is True


def test_no_budget_means_no_notice():
    """The CLI, cron, and the evals never fanned out and have nothing to spend."""
    assert tools._budget_notice(make_ctx(ForecastDeps())) == ""


def test_an_errored_search_still_costs_a_call(monkeypatch):
    """Counted at the result, matching what pydantic-ai's own UsageLimits counts."""
    deps = budgeted()

    async def boom(ctx, query):
        return "Web search error: connection refused"

    monkeypatch.setattr(tools, "_search_web", boom)
    import asyncio

    asyncio.run(tools.search_web(make_ctx(deps), "anything"))
    assert deps.budget.used == 1
