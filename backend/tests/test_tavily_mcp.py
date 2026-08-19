"""Tests for the live search path — Tavily's MCP server.

Two failures here would be silent. If `web_search_toolset` stopped branching on `as_of`, a
backtest would search through the MCP server, which cannot filter undated results, and every
scorecard would still print green. If `process_tool_call` stopped recording sources,
`check_citations` would fail every forecast that cites a URL — loud, but only in production.

No network: an in-process `FastMCP` server stands in for Tavily, so the whole path from
argument capping to `sources_seen` runs for real against a fake server.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastmcp import FastMCP
from pydantic_ai import RunContext
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset, RenamedToolset

from superforecaster import tavily_mcp
from superforecaster.agents.critic import build_critic_agent
from superforecaster.config import Budget
from superforecaster.deps import ForecastDeps

AS_OF = datetime(2022, 2, 1, tzinfo=timezone.utc)

# Every name here is the literal the live server serves, never `tavily_mcp.SEARCH_TOOL`.
# The constant said "tavily-search" for a while, the server has always said "tavily_search",
# and a fixture named from the constant agreed with the bug: the suite was green while
# production renamed nothing, capped nothing, and recorded no sources at all.
SEARCH = "tavily_search"
RESEARCH = "tavily_research"
CRAWL = "tavily_crawl"

# The real response shape, taken from a live call: a JSON object with the hits under
# `results`, and no `published_date` on any of them.
SEARCH_RESULT = {
    "query": "fed rate cut odds",
    "answer": None,
    "images": [],
    "results": [
        {
            "url": "https://example.com/fed-holds",
            "title": "Fed holds rates steady",
            "content": "The committee left the target range unchanged.",
            "score": 0.9,
        },
        {
            "url": "https://example.org/analysts",
            "title": "Analysts split on timing",
            "content": "Forecasters disagree about the first cut.",
            "score": 0.7,
        },
    ],
}

# `tavily_research` puts its citations under `sources` instead.
RESEARCH_RESULT = {
    "content": "# OpenAI IPO Overview\n\nThe filing landed in June [1].",
    "sources": [{"url": "https://example.net/ipo", "title": "OpenAI files S-1"}],
    "status": "completed",
}


@pytest.fixture
def tavily_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")


@pytest.fixture
def fake_server(monkeypatch):
    """An in-process MCP server that answers like Tavily, and records what it was sent."""
    captured: dict = {}
    server = FastMCP("fake-tavily")

    @server.tool(name=SEARCH)
    def search(
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        include_raw_content: bool = False,
        include_images: bool = False,
    ) -> dict:
        captured.update(
            tool=SEARCH,
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_raw_content=include_raw_content,
            include_images=include_images,
        )
        return SEARCH_RESULT

    @server.tool(name=RESEARCH)
    def research(input: str, model: str = "auto") -> dict:
        captured.update(tool=RESEARCH, input=input, model=model)
        return RESEARCH_RESULT

    toolset = MCPToolset[ForecastDeps](
        server, id="tavily", process_tool_call=tavily_mcp.process_tool_call
    )
    monkeypatch.setattr(tavily_mcp, "_toolsets", {"tvly-test": toolset})
    return captured


def context(deps: ForecastDeps) -> RunContext[ForecastDeps]:
    return RunContext(deps=deps, model=None, usage=None, prompt=None)


# ---------- parse_sources ----------


def test_parse_sources_reads_the_results_array():
    refs = tavily_mcp.parse_sources(SEARCH_RESULT, query="fed", tool="search_web")
    assert [r.url for r in refs] == [
        "https://example.com/fed-holds",
        "https://example.org/analysts",
    ]
    assert refs[0].title == "Fed holds rates steady"
    assert refs[0].query == "fed"


def test_parse_sources_reads_the_sources_array_that_research_returns():
    """Research names the same idea `sources`, so one parser has to read both."""
    refs = tavily_mcp.parse_sources(RESEARCH_RESULT, query="ipo", tool=RESEARCH)
    assert [r.url for r in refs] == ["https://example.net/ipo"]


def test_parse_sources_accepts_a_json_string_as_well_as_a_dict():
    refs = tavily_mcp.parse_sources(
        json.dumps(SEARCH_RESULT), query="fed", tool="search_web"
    )
    assert len(refs) == 2


def test_parse_sources_leaves_published_date_unset():
    """No MCP tool returns one, so nothing here may invent one."""
    refs = tavily_mcp.parse_sources(SEARCH_RESULT, query="fed", tool="search_web")
    assert all(r.published_date is None for r in refs)


def test_parse_sources_survives_a_body_it_cannot_read():
    assert (
        tavily_mcp.parse_sources("upstream is down", query="q", tool="search_web") == []
    )


def test_parse_sources_skips_a_result_with_no_url():
    body = {"results": [{"title": "no link"}, {"url": "https://example.com/a"}]}
    refs = tavily_mcp.parse_sources(body, query="q", tool="search_web")
    assert [r.url for r in refs] == ["https://example.com/a"]


# ---------- argument capping ----------


def test_search_arguments_are_capped_whatever_the_model_asks_for():
    args = tavily_mcp._capped(
        SEARCH, {"query": "q", "max_results": 20, "search_depth": "advanced"}
    )
    assert args["max_results"] == 5
    assert args["search_depth"] == "basic"
    assert args["include_raw_content"] is False


def test_a_smaller_request_is_left_alone():
    """`_BOUNDED` is a ceiling, not an assignment — an agent may still ask for less."""
    assert (
        tavily_mcp._capped(SEARCH, {"query": "q", "max_results": 2})["max_results"] == 2
    )


def test_research_depth_is_pinned_not_left_to_the_model():
    """`model="auto"` once ran 176s against a 180s timeout. The tier is our choice."""
    assert (
        tavily_mcp._capped(RESEARCH, {"input": "q", "model": "pro"})["model"] == "mini"
    )
    assert tavily_mcp._capped(RESEARCH, {"input": "q"})["model"] == "mini"


def test_crawl_is_capped_on_every_axis_that_multiplies():
    args = tavily_mcp._capped(
        CRAWL, {"url": "https://x", "limit": 500, "max_depth": 5, "max_breadth": 99}
    )
    assert (args["limit"], args["max_depth"], args["max_breadth"]) == (10, 1, 10)


def test_capping_an_unknown_tool_changes_nothing():
    args = {"whatever": 1}
    assert tavily_mcp._capped("tavily_unknown", args) == args


# ---------- process_tool_call, against the fake server ----------


async def test_a_search_records_every_result_on_deps(tavily_key, fake_server):
    deps = ForecastDeps()
    await tavily_mcp.mcp_search(context(deps), "fed rate cut")

    assert [s.url for s in deps.sources_seen] == [
        "https://example.com/fed-holds",
        "https://example.org/analysts",
    ]
    assert {s.tool for s in deps.sources_seen} == {"search_web"}


async def test_the_server_is_sent_the_capped_arguments(tavily_key, fake_server):
    await tavily_mcp.mcp_search(context(ForecastDeps()), "fed rate cut")
    assert fake_server["max_results"] == 5
    assert fake_server["include_raw_content"] is False


async def test_a_search_error_is_not_an_exception(tavily_key, monkeypatch):
    """Same contract as `tools.search_web` — a dead upstream is missing information."""

    class Broken:
        async def direct_call_tool(self, *a, **kw):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(tavily_mcp, "_toolsets", {"tvly-test": Broken()})
    result = await tavily_mcp.mcp_search(context(ForecastDeps()), "q")
    assert "Web search error" in result


async def test_research_records_its_citations_and_is_sent_the_mini_tier(
    tavily_key, fake_server
):
    deps = ForecastDeps()
    await tavily_mcp.process_tool_call(
        context(deps),
        tavily_mcp._toolset("tvly-test").direct_call_tool,
        RESEARCH,
        {"input": "OpenAI IPO"},
    )
    assert fake_server["model"] == "mini"
    assert [s.url for s in deps.sources_seen] == ["https://example.net/ipo"]
    assert deps.sources_seen[0].query == "OpenAI IPO"


async def test_a_url_tool_falls_back_to_what_it_was_pointed_at(tavily_key):
    """A body with no source array still leaves an audit trail."""
    deps = ForecastDeps()

    async def call_tool(name, args, *, metadata=None):
        return "extracted text"

    await tavily_mcp.process_tool_call(
        context(deps), call_tool, "tavily_extract", {"urls": ["https://example.com/a"]}
    )
    assert [s.url for s in deps.sources_seen] == ["https://example.com/a"]
    assert deps.sources_seen[0].tool == "tavily_extract"


# ---------- which toolset a run gets ----------


def test_a_backtest_gets_the_audited_http_tool_not_the_mcp_server(tavily_key):
    """The branch ADR 17 depends on. MCP results carry no date, so `_drop_leaked` cannot run."""
    toolset = tavily_mcp.web_search_toolset(context(ForecastDeps(as_of=AS_OF)))
    assert isinstance(toolset, FunctionToolset)


def test_a_live_run_gets_the_mcp_server(tavily_key, fake_server):
    toolset = tavily_mcp.web_search_toolset(context(ForecastDeps()))
    assert isinstance(toolset, RenamedToolset)


def test_no_key_offers_no_web_tools_at_all(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert tavily_mcp.web_search_toolset(context(ForecastDeps())) is None


def test_the_key_never_reaches_the_url_from_a_module_constant(tavily_key):
    """It is read per call, so `PUT /config/keys` takes effect without a restart."""
    assert "tavilyApiKey=tvly-test" in tavily_mcp._server_url("tvly-test")


# ---------- the whole path, through a real agent ----------


async def test_an_agent_calls_the_mcp_tool_under_the_name_the_prompts_use(
    tavily_key, fake_server
):
    """`tavily-search` must reach the model as `search_web`.

    Every prompt names that tool, `SourceRef.tool` carries it to the frontend, and
    `agents.attach_budget` tells the agent to stop calling it by name. The rename is what
    lets the MCP server slot in under all of them.
    """
    agent = build_critic_agent()
    deps = ForecastDeps()

    with agent.override(model=TestModel(call_tools=["search_web"])):
        await agent.run("Is this question well posed?", deps=deps)

    assert fake_server["query"], "the fake server was never called"
    assert [s.url for s in deps.sources_seen] == [
        "https://example.com/fed-holds",
        "https://example.org/analysts",
    ]


async def test_a_spent_budget_withdraws_the_mcp_tool_too(tavily_key, fake_server):
    """ADR 69 has to keep working now that the tool comes from a toolset, not `tools=[]`.

    `withdraw_spent_tools` is a `prepare_tools` hook. If it only reached function tools, a
    cell could search past its budget, hit `tool_calls_limit`, and return nothing at all.
    """
    agent = build_critic_agent()
    deps = ForecastDeps(
        budget=Budget(
            name="critic", cost_usd=1.0, tokens=100_000, tool_calls=0, iterations=5
        )
    )

    with agent.override(model=TestModel()):
        result = await agent.run("Is this question well posed?", deps=deps)

    assert "search_web" not in [
        part.tool_name
        for message in result.all_messages()
        for part in message.parts
        if hasattr(part, "tool_name")
    ]
    assert deps.sources_seen == []
