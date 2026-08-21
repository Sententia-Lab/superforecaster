"""Two rules every `Agent(...)` constructor in `agents/` has to satisfy.

Both were real defects first. A tool added without `capabilities=[Hooks(prepare_tools=
withdraw_tools)]` is offered against an empty research store and never withdrawn when the
budget runs out. An agent that can call tools but was not given `search_research` cannot
read what the run already fetched, and pays a search to re-fetch it.

Read off the constructors rather than a list here, for the reason `test_config`'s
`_agent_tool_use` gives: a list has to be edited whenever an agent gains a toolset, and
forgetting to edit it *is* the bug the test exists to catch.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

AGENTS_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "superforecaster" / "agents"
)


def _agent_calls() -> list[tuple[str, str, list[str], str]]:
    """(file, agent name, tool names, the constructor's source) per `Agent(...)` built."""
    out = []
    for path in sorted(AGENTS_DIR.glob("*.py")):
        source = path.read_text()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            is_agent = (isinstance(fn, ast.Name) and fn.id == "Agent") or (
                isinstance(fn, ast.Subscript)
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "Agent"
            )
            if not is_agent:
                continue
            kwargs = {k.arg: k.value for k in node.keywords}
            name = getattr(kwargs.get("name"), "value", path.stem)
            tools = [
                t.id
                for t in getattr(kwargs.get("tools"), "elts", [])
                if isinstance(t, ast.Name)
            ]
            out.append(
                (path.name, name, tools, ast.get_source_segment(source, node) or "")
            )
    return out


AGENT_CALLS = _agent_calls()


def test_the_scan_found_every_agent():
    """A guard on the guard: an AST walk that silently matches nothing passes everything."""
    assert len(AGENT_CALLS) >= 11


@pytest.mark.parametrize(
    "file,name,tools,src", AGENT_CALLS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_an_agent_with_tools_can_have_them_withdrawn(file, name, tools, src):
    """`withdraw_tools` is the only place a tool stops being offered — no `TAVILY_API_KEY`,
    an empty research store, a spent budget. An agent that skips the hook answers all three
    questions "yes" forever, and pays a tool call to find out otherwise."""
    if not tools:
        return
    assert (
        "prepare_tools=withdraw_tools" in src
    ), f"{file}: agent {name!r} offers {tools} with no withdraw_tools hook"


@pytest.mark.parametrize(
    "file,name,tools,src", AGENT_CALLS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_an_agent_that_can_call_tools_can_read_the_research_store(
    file, name, tools, src
):
    """Every agent that can call a tool can read what this run already fetched."""
    if not tools:
        return
    assert (
        "search_research" in tools
    ), f"{file}: agent {name!r} has tools but cannot read the research store"
