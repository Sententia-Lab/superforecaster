"""The research store as the agents see it: written by the search tools, read by one tool.

The store only pays off if the pages actually get in and the tool is offered when there is
something to find. Both are asserted here; how the store itself ranks and deletes is
`test_research_store`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.usage import RunUsage

from app import db, research
from superforecaster.agents import withdraw_tools
from superforecaster.config import get_budget
from superforecaster.deps import ForecastDeps
from superforecaster.tools import research_store_tools, tavily_tools
from superforecaster.models import ResearchDoc


class _Ctx:
    """The part of `RunContext` these paths read: deps and usage."""

    def __init__(self, deps: ForecastDeps, tool_calls: int = 0):
        self.deps = deps
        self.usage = RunUsage(tool_calls=tool_calls)


@dataclass
class _Tool:
    name: str


def _names(defs: list[Any]) -> set[str]:
    return {d.name for d in defs}


ALL_TOOLS = [_Tool("search_web"), _Tool("search_research"), _Tool("search_wikipedia")]


@pytest.fixture
def store():
    return research.new_store()


# ---------- the pages get in ----------


async def test_search_web_stores_what_it_fetched(monkeypatch, store):
    """The page text is already paid for; today it is read once and dropped."""

    class _Client:
        async def search(self, query, **kwargs):
            return {
                "results": [
                    {
                        "url": "https://ustr.gov/exclusions",
                        "title": "Steel tariff exclusions",
                        "content": "The exclusion process for steel was renewed.",
                    }
                ]
            }

    monkeypatch.setattr(tavily_tools, "_client", lambda: _Client())
    deps = ForecastDeps(store=store)

    await tavily_tools.search_web(_Ctx(deps), "steel exclusions")

    hits = store.find("steel exclusions")
    assert [h.url for h in hits] == ["https://ustr.gov/exclusions"]
    assert "renewed" in hits[0].content


async def test_extract_pages_stores_the_full_page(monkeypatch, store):
    """`extract_pages` keeps `raw_content`, not the `content` excerpt `search_web` gets.

    Its own test because the two tools read different keys off different Tavily shapes,
    and `_remember` swallows its errors — a wrong key here stores empty bodies silently.
    """

    class _Client:
        async def extract(self, urls, **kwargs):
            return {
                "results": [
                    {
                        "url": "https://ustr.gov/full",
                        "title": "The whole notice",
                        "raw_content": "Every paragraph of the exclusion notice.",
                    }
                ]
            }

    monkeypatch.setattr(tavily_tools, "_client", lambda: _Client())
    deps = ForecastDeps(store=store)

    await tavily_tools.extract_pages(_Ctx(deps), ["https://ustr.gov/full"])

    hits = store.find("exclusion notice paragraph")
    assert [h.url for h in hits] == ["https://ustr.gov/full"]
    assert hits[0].content == "Every paragraph of the exclusion notice."


async def test_a_body_the_upstream_did_not_send_is_not_stored_as_text(
    monkeypatch, store
):
    """A result missing its body key stores an empty body, not the string "None"."""

    class _Client:
        async def search(self, query, **kwargs):
            return {"results": [{"url": "https://a", "title": "Only a title"}]}

    monkeypatch.setattr(tavily_tools, "_client", lambda: _Client())

    await tavily_tools.search_web(_Ctx(ForecastDeps(store=store)), "q")

    assert store.find("title")[0].content == ""


async def test_no_store_means_no_write(monkeypatch):
    """A run without a store behaves exactly as it did before the store existed."""

    class _Client:
        async def search(self, query, **kwargs):
            return {"results": [{"url": "https://a", "title": "t", "content": "c"}]}

    monkeypatch.setattr(tavily_tools, "_client", lambda: _Client())
    deps = ForecastDeps(store=None)

    out = await tavily_tools.search_web(_Ctx(deps), "anything")

    assert "https://a" in out
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM research_docs").fetchone()[0] == 0


# ---------- reading them back ----------


async def test_the_tool_returns_stored_pages(store):
    store.remember(
        [
            ResearchDoc(
                url="https://ustr.gov/exclusions",
                title="Steel tariff exclusions",
                body="The exclusion process for steel was renewed in 2026.",
            )
        ]
    )
    deps = ForecastDeps(store=store)

    out = await research_store_tools.search_research(_Ctx(deps), "steel exclusions")

    assert "ustr.gov/exclusions" in out
    assert "research_store" in out


async def test_a_read_is_recorded_as_a_source(store):
    """`checks.check_citations` fails a forecast citing a URL absent from this list, so a
    page read back from the store has to land there like any other."""
    store.remember([ResearchDoc(url="https://a", title="A", body="alpha")])
    deps = ForecastDeps(store=store)

    await research_store_tools.search_research(_Ctx(deps), "alpha")

    assert [(s.url, s.tool) for s in deps.sources_seen] == [
        ("https://a", "research_store")
    ]


async def test_an_empty_store_says_so_rather_than_failing(store):
    out = await research_store_tools.search_research(
        _Ctx(ForecastDeps(store=store)), "x"
    )
    assert "Nothing stored yet" in out


async def test_no_store_at_all_says_so(store):
    out = await research_store_tools.search_research(_Ctx(ForecastDeps()), "x")
    assert "unavailable" in out


# ---------- a saved forecast keeps its store ----------


def test_a_saved_forecast_carries_its_research_id():
    """`update`, `resolution`, and `postmortem` run months after the run that made the
    forecast. The record is the only thing that still knows which store was its."""
    from tests.test_db_forecasts import _make_forecast

    fid = db.save_forecast(_make_forecast(), resolution_source="x", research_id="run-7")

    assert db.get_forecast(fid).research_id == "run-7"


def test_store_for_a_forecast_that_kept_none():
    """A forecast saved before the store existed. The tool is withdrawn, nothing raises."""
    assert research.store_for(None) is None


async def test_the_refresh_reaches_the_original_research(monkeypatch):
    """The whole point of putting `research_id` on the forecast: a refresh can read what
    the forecast was built on rather than searching for it again."""
    from tests.test_db_forecasts import _make_forecast
    from app import update as update_graph

    store = research.new_store()
    store.remember([ResearchDoc(url="https://a", title="A", body="alpha")])
    fid = db.save_forecast(
        _make_forecast(), resolution_source="x", research_id=store.research_id
    )

    seen = {}

    async def fake_cycle(record, deps):
        seen["store"] = deps.store
        from superforecaster.models import UpdateOutcome

        return UpdateOutcome(reason="stub")

    monkeypatch.setattr(update_graph, "run_update_cycle", fake_cycle)
    await update_graph.run_update_graph(fid)

    assert seen["store"] is not None
    assert [h.url for h in seen["store"].find("alpha")] == ["https://a"]


# ---------- when it is offered ----------


async def test_withdrawn_while_the_store_is_empty(store, monkeypatch):
    """The base-rate stage starts against an empty store and runs its cells at once.

    Offering a tool that can only answer "nothing yet" costs a tool call to learn what
    the hook already knows.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    ctx = _Ctx(ForecastDeps(store=store, budget=get_budget("base_rate_cell")))

    assert "search_research" not in _names(await withdraw_tools(ctx, ALL_TOOLS))


async def test_offered_once_documents_exist(store, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    store.remember([ResearchDoc(url="https://a", title="A", body="alpha")])
    ctx = _Ctx(ForecastDeps(store=store, budget=get_budget("base_rate_cell")))

    assert "search_research" in _names(await withdraw_tools(ctx, ALL_TOOLS))


async def test_withdrawn_when_there_is_no_store(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    ctx = _Ctx(ForecastDeps(budget=get_budget("base_rate_cell")))

    assert "search_research" not in _names(await withdraw_tools(ctx, ALL_TOOLS))


async def test_a_store_read_spends_a_tool_call(store, monkeypatch):
    """It is an ordinary tool call, with no counter and no exemption. The budgets were
    raised to pay for it instead."""
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    store.remember([ResearchDoc(url="https://a", title="A", body="alpha")])
    budget = get_budget("base_rate_cell")
    deps = ForecastDeps(store=store, budget=budget)

    assert _names(await withdraw_tools(_Ctx(deps, budget.tool_calls - 1), ALL_TOOLS))
    assert await withdraw_tools(_Ctx(deps, budget.tool_calls), ALL_TOOLS) == []
