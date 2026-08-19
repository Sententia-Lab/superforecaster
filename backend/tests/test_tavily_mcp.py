"""Tests for the live search path — Tavily's MCP server.

Two failures here would be silent. If `web_search_toolset` stopped branching on `as_of`, a
backtest would search through the MCP server, which cannot filter undated results, and every
scorecard would still print green. If `process_tool_call` stopped recording sources,
`check_citations` would fail every forecast that cites a URL — loud, but only in production.

No network: an in-process `FastMCP` server stands in for Tavily, so the whole path from
argument capping to `sources_seen` runs for real against a fake server.
"""

from __future__ import annotations

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

# What `formatResults()` in the Tavily MCP server prints. Titles and URLs on their own
# lines, and no publication date anywhere — which is the whole reason a backtest cannot
# use this path.
RESULT_TEXT = """Detailed Results:

Title: Fed holds rates steady
URL: https://example.com/fed-holds
Content: The committee left the target range unchanged.

Title: Analysts split on timing
URL: https://example.org/analysts
Content: Forecasters disagree about the first cut.
"""


@pytest.fixture
def tavily_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")


@pytest.fixture
def fake_server(monkeypatch):
    """An in-process MCP server that answers like Tavily, and records what it was sent."""
    captured: dict = {}
    server = FastMCP("fake-tavily")

    @server.tool(name=tavily_mcp.SEARCH_TOOL)
    def search(
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        include_raw_content: bool = False,
        include_images: bool = False,
    ) -> str:
        captured.update(
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_raw_content=include_raw_content,
            include_images=include_images,
        )
        return RESULT_TEXT

    toolset = MCPToolset[ForecastDeps](
        server, id="tavily", process_tool_call=tavily_mcp.process_tool_call
    )
    monkeypatch.setattr(tavily_mcp, "_toolsets", {"tvly-test": toolset})
    return captured


def context(deps: ForecastDeps) -> RunContext[ForecastDeps]:
    return RunContext(deps=deps, model=None, usage=None, prompt=None)


# ---------- parse_sources ----------


def test_parse_sources_pairs_every_title_with_its_url():
    refs = tavily_mcp.parse_sources(RESULT_TEXT, query="fed", tool="search_web")
    assert [r.url for r in refs] == [
        "https://example.com/fed-holds",
        "https://example.org/analysts",
    ]
    assert refs[0].title == "Fed holds rates steady"
    assert refs[0].query == "fed"


def test_parse_sources_leaves_published_date_unset():
    """The server discards it, so nothing here may invent one."""
    refs = tavily_mcp.parse_sources(RESULT_TEXT, query="fed", tool="search_web")
    assert all(r.published_date is None for r in refs)


def test_parse_sources_survives_a_body_it_cannot_read():
    assert (
        tavily_mcp.parse_sources("upstream is down", query="q", tool="search_web") == []
    )


def test_parse_sources_keeps_a_url_that_has_no_title():
    refs = tavily_mcp.parse_sources(
        "URL: https://example.com/a", query="q", tool="search_web"
    )
    assert len(refs) == 1 and refs[0].title == ""


# ---------- argument capping ----------


def test_search_arguments_are_capped_whatever_the_model_asks_for():
    args = tavily_mcp._capped(
        tavily_mcp.SEARCH_TOOL,
        {"query": "q", "max_results": 20, "search_depth": "advanced"},
    )
    assert args["max_results"] == tavily_mcp.MAX_RESULTS
    assert args["search_depth"] == "basic"
    assert args["include_raw_content"] is False


def test_crawl_depth_is_capped():
    assert (
        tavily_mcp._capped("tavily-crawl", {"url": "https://x", "limit": 500})["limit"]
        == tavily_mcp.MAX_CRAWL
    )


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
    assert fake_server["max_results"] == tavily_mcp.MAX_RESULTS
    assert fake_server["include_raw_content"] is False


async def test_a_search_error_is_not_an_exception(tavily_key, monkeypatch):
    """Same contract as `tools.search_web` — a dead upstream is missing information."""

    class Broken:
        async def direct_call_tool(self, *a, **kw):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(tavily_mcp, "_toolsets", {"tvly-test": Broken()})
    result = await tavily_mcp.mcp_search(context(ForecastDeps()), "q")
    assert "Web search error" in result


async def test_a_url_tool_records_what_it_was_pointed_at(tavily_key):
    deps = ForecastDeps()

    async def call_tool(name, args, *, metadata=None):
        return "extracted text"

    await tavily_mcp.process_tool_call(
        context(deps), call_tool, "tavily-extract", {"urls": ["https://example.com/a"]}
    )
    assert [s.url for s in deps.sources_seen] == ["https://example.com/a"]
    assert deps.sources_seen[0].tool == "tavily-extract"


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
