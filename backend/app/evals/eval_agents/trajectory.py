"""Grade the path an agent took, not just where it arrived."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from pydantic import BaseModel, Field
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_evals.dataset import set_eval_attribute
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext

TRAJECTORY = "trajectory"
"""Attribute name the recorder writes and the judge reads."""

OUTPUT_TOOL = "final_result"
"""Pydantic AI delivers the structured answer as a tool call by this name. The agent did
not choose it, so it is dropped — counting it would add one call to every trajectory."""

RESULT_CHARS = 600
"""Tool returns are truncated to this. A Tavily response is the largest thing in the
transcript, and the judge is grading the query, not the search engine."""


# ---------- recording ----------


def trajectory_events(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    """Flatten a run's messages into ordered events."""
    events: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, (TextPart, ThinkingPart)) and part.content.strip():
                    events.append({"kind": "reasoning", "text": part.content.strip()})
                elif isinstance(part, ToolCallPart) and part.tool_name != OUTPUT_TOOL:
                    events.append(
                        {
                            "kind": "call",
                            "tool": part.tool_name,
                            "args": part.args_as_dict(),
                        }
                    )
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name != OUTPUT_TOOL:
                    events.append(
                        {
                            "kind": "result",
                            "tool": part.tool_name,
                            "text": _clip(part.content),
                        }
                    )
                elif isinstance(part, RetryPromptPart):
                    events.append(
                        {
                            "kind": "retry",
                            "tool": part.tool_name or "",
                            "text": _clip(part.content),
                        }
                    )
    return events


def _clip(content: Any) -> str:
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    if len(text) <= RESULT_CHARS:
        return text
    return f"{text[:RESULT_CHARS]}... [{len(text) - RESULT_CHARS} more characters]"


@contextmanager
def record_trajectory() -> Iterator[None]:
    """Record one agent run on the current case, for `ToolTrajectoryJudge` to read."""
    with capture_run_messages() as messages:
        try:
            yield
        finally:
            set_eval_attribute(TRAJECTORY, trajectory_events(messages))


# ---------- judging ----------


def render_trajectory(events: list[dict[str, Any]]) -> str:
    """The transcript the judge reads. Calls are numbered; nothing else is."""
    if not events:
        return "(the agent called no tools and wrote no reasoning)"

    lines: list[str] = []
    call = 0
    for event in events:
        kind = event["kind"]
        if kind == "reasoning":
            lines.append(f"REASONING: {event['text']}")
        elif kind == "call":
            call += 1
            lines.append(
                f"CALL {call}: {event['tool']}({json.dumps(event['args'], default=str)})"
            )
        elif kind == "result":
            lines.append(f"RETURNED: {event['text']}")
        elif kind == "retry":
            lines.append(f"REJECTED — the call was retried: {event['text']}")
    lines.append(f"\nTOTAL TOOL CALLS: {call}")
    return "\n".join(lines)


class TrajectoryVerdict(BaseModel):
    """Three scores, because the three failures are independent."""

    tool_selection: float = Field(
        ge=0.0,
        le=1.0,
        description="Was each call the right tool for what the agent was trying to "
        "learn, and was calling a tool at all the right move at that point?",
    )
    tool_selection_reason: str = Field(
        description="Name the call number and the tool for every choice you marked down. "
        "One or two sentences."
    )
    parameters: float = Field(
        ge=0.0,
        le=1.0,
        description="Were the arguments aimed at the thing the agent needed? Quote a "
        "weak argument and say what it should have been.",
    )
    parameters_reason: str = Field(description="One or two sentences.")
    call_count: float = Field(
        ge=0.0,
        le=1.0,
        description="Was the number of calls right for this task — no repeats, no "
        "searching for something already returned, and not too few to answer?",
    )
    call_count_reason: str = Field(description="One or two sentences.")


JUDGE_INSTRUCTIONS = """You grade the tool calls an agent made while doing a task. You do
not grade the answer it produced — a different judge does that. You are reading the path.

You get the task, a transcript of the run, the final output, and a rubric that says what
this agent's tools are and what it was told about using them.

Score three things from 0.0 to 1.0, each on its own:

  tool_selection  Was each call the right tool, and was calling a tool the right move at
                  that point? A call that goes after information the agent was told not to
                  look for scores low here even if the tool was the only one available.
  parameters      Were the arguments aimed at what the agent needed? A vague query that
                  happened to return something useful is still a vague query.
  call_count      Was the number right for THIS task? Repeats, near-duplicate queries, and
                  searching for something an earlier return already gave all score low. So
                  does stopping too early when the task plainly needed a lookup.

Zero tool calls is a real trajectory, not an automatic failure. Score it on whether this
task needed a call. If it did not, zero calls is 1.0 across all three.

Judge what the agent knew at the time. A search that returned nothing useful is not a bad
search if it was a sensible thing to look for before the results came back.

Give every reason a specific anchor: a call number, a quoted argument, or a quoted line of
the agent's reasoning. "The queries were vague" is not a usable reason."""

_agents: dict[str | None, Agent[None, TrajectoryVerdict]] = {}


def _judge_agent(model: str | None) -> Agent[None, TrajectoryVerdict]:
    if model not in _agents:
        _agents[model] = Agent(
            model=model,
            name="trajectory_judge",
            output_type=TrajectoryVerdict,
            system_prompt=JUDGE_INSTRUCTIONS,
        )
    return _agents[model]


@dataclass
class ToolTrajectoryJudge(Evaluator[object, object, object]):
    """Score an agent's tool use with a second model."""

    rubric: str
    model: str | None = None

    async def evaluate(
        self, ctx: EvaluatorContext[object, object, object]
    ) -> dict[str, EvaluationReason]:
        events = ctx.attributes.get(TRAJECTORY)
        if events is None:
            raise ValueError(
                "no trajectory on this case — wrap the agent call in "
                "`with record_trajectory():` inside the task function"
            )

        prompt = (
            f"<Task>\n{_stringify(ctx.inputs)}\n</Task>\n\n"
            f"<Trajectory>\n{render_trajectory(events)}\n</Trajectory>\n\n"
            f"<FinalOutput>\n{_stringify(ctx.output)}\n</FinalOutput>\n\n"
            f"<Rubric>\n{self.rubric}\n</Rubric>"
        )
        verdict = (await _judge_agent(self.model).run(prompt)).output
        return {
            "tool_selection": EvaluationReason(
                value=verdict.tool_selection, reason=verdict.tool_selection_reason
            ),
            "tool_parameters": EvaluationReason(
                value=verdict.parameters, reason=verdict.parameters_reason
            ),
            "tool_call_count": EvaluationReason(
                value=verdict.call_count, reason=verdict.call_count_reason
            ),
        }


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    return json.dumps(value, indent=2, default=str)
