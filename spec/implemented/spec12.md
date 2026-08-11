# Spec 12 — Grading the path, not just the answer

**Status: implemented** (August 2026). The harness ships; it has not been run against a
real model yet, so no scores exist.

## Why

Six of the eleven agents carry tools:

| Agent | Tools |
|---|---|
| `critic` | `search_web` |
| `outside_view` | `search_web`, `search_wikipedia` |
| `inside_view` | `search_web`, `search_wikipedia`, `find_disconfirming_evidence` |
| `update` | `search_web`, `find_disconfirming_evidence` |
| `resolution` | `search_web`, `search_wikipedia` |
| `postmortem` | `search_web` |

Every eval in the repo reads the final output and nothing else. Two critic runs can
return the same `CriteriaCritique` and be worth different amounts:

- Run A searches once for "ONS Consumer Price Inflation bulletin", confirms the bulletin
  publishes the number, and names it.
- Run B searches three times — twice for the answer to the question, which its own
  instructions forbid — and names the same bulletin.

`score_critic` in `app/evals/components.py` gives both runs the same score. Run B costs
three times as much and ignored its prompt.

**Outcome:** an eval that scores the tool calls an agent made, the arguments it passed,
and how many calls it took to get there.

## What pydantic-evals already gives

`pydantic_evals.evaluators.agentic`, added in 2.x, reads the OTel span tree. Its own
docstring: *"They are deterministic and require no LLM calls."*

| Evaluator | Needs from you | Fits the critic? |
|---|---|---|
| `MaxToolCalls(max_calls)` | a number | **yes** — use it, do not hand-roll a counter |
| `MaxModelRequests(max_requests)` | a number | **yes** |
| `ToolCorrectness(expected_tools)` | the expected tool names | barely — the critic has one tool. Real value on `inside_view` (three) and `update` (two) |
| `TrajectoryMatch(expected_trajectory)` | the expected ordered names | no — see "What this does not do" |
| `ArgumentCorrectness(tool_name, expected_arguments)` | an expected argument **dict** | no — `search_web(query=...)` is free text, and there is no dict to write |

So the mechanical half is solved and the judging half is not. Three things this eval needs
to score have no ground truth to assert against:

- Was a search worth making at all, given what the agent had already read?
- Was the query aimed at the right thing, or fishing?
- Was the count right *for this question* — one that already names the BLS needs a search
  that one saying "significant adoption" does not.

None of those are a list or a dict. All three depend on the model's reasoning, which is
not in a span at all.

| Source | Gives |
|---|---|
| `ctx.span_tree` | tool name, call id, timings, arguments (with `include_content=True`), nested sub-agent calls |
| `pydantic_ai.capture_run_messages()` | ordered calls, validated arguments, tool returns, **and the text the model wrote between them** |

The agentic evaluators take the first. The judge takes the second, for the reasoning.

## Where it sits

```
app/evals/
  components.py       per-agent scorers, output only          (unchanged)
  decompose_eval.py   pydantic-evals dataset, output only     (unchanged)
  trajectory.py       NEW — record the run, judge the run
  critic_eval.py      NEW — the first dataset that uses it
```

`trajectory.py` is agent-neutral. Each agent's eval supplies its own rubric, the same way
each supplies its own `LLMJudge` rubric today.

```
                 ┌─────────────────────────────┐
   make eval     │ critic_eval.py              │
   critic  ─────>│   CASES + RUBRIC + TOOL_RUBRIC
                 └──────────────┬──────────────┘
                                │ Dataset.evaluate_sync(task)
                                v
                 ┌─────────────────────────────┐
                 │ task()                      │
                 │   record_trajectory():      │  <- trajectory.py
                 │     run_critique(...)       │
                 └──────────────┬──────────────┘
                                │ set_eval_attribute("trajectory", events)
                                v
        ┌───────────────────────┴───────────────────────┐
        │                       │                       │
   Verdict()             NamedASource()        ToolTrajectoryJudge()
   mechanical            mechanical            second model  <- trajectory.py
   reads ctx.output      reads ctx.output      reads ctx.attributes
```

## Data lineage

```
make eval critic
```

**1. The case.** `inputs` is a `CriticInput`, not a `ForecastInput` — the critic takes
loose fields, not an assembled forecast.

```json
{
  "name": "vague_adoption",
  "inputs": {
    "question": "Will electric vehicles see significant adoption in Norway by 2027?",
    "resolution_criteria": "YES if EV adoption in Norway is significant by 2027-01-01.",
    "resolution_date": "2027-01-01T00:00:00Z"
  },
  "metadata": {"is_resolvable": false, "max_calls": 2}
}
```

**2. The task runs the agent inside the recorder.**

```
task(inputs: CriticInput) -> CriteriaCritique
  record_trajectory()                                  [app/evals/trajectory.py]
    -> capture_run_messages() as messages
    -> run_critique(question, resolution_criteria, resolution_date, deps)
    -> trajectory_events(messages) -> list[dict]
    -> set_eval_attribute("trajectory", events)        [writes: the case's attributes]
```

**3. `trajectory_events` flattens the messages into ordered events.** The output tool
call (`final_result`) is dropped: Pydantic AI adds it to deliver the structured answer, so
it is not a tool the agent chose.

```json
[
  {"kind": "reasoning", "text": "The criteria say \"significant\" with no threshold and name no source. I need to check whether a Norwegian body publishes EV registration shares I could point at."},
  {"kind": "call", "tool": "search_web", "args": {"query": "Norway EV new car registration share statistics official"}},
  {"kind": "result", "tool": "search_web", "text": "Opplysningsradet for Veitrafikken (OFV) publishes monthly new registration figures by fuel type ..."},
  {"kind": "reasoning", "text": "OFV is the adjudicator. I can write a threshold against its monthly figure."}
]
```

**4. The judge reads the events, the task, and the final output.**

```
ToolTrajectoryJudge.evaluate(ctx) -> dict[str, EvaluationReason]
  -> ctx.attributes["trajectory"]
  -> render_trajectory(events) -> str
  -> judge_agent.run(prompt) -> TrajectoryVerdict
```

The prompt the judge model receives:

```
<Task> ... the question and criteria ... </Task>
<Trajectory> ... the rendered events ... </Trajectory>
<FinalOutput> ... the CriteriaCritique as JSON ... </FinalOutput>
<Rubric> ... TOOL_RUBRIC from critic_eval.py ... </Rubric>
```

**5. The verdict.** Three scores, because the three things go wrong independently. A
perfect query passed to the wrong tool is not the same failure as the right tool called
five times.

```json
{
  "tool_selection": 1.0,
  "tool_selection_reason": "search_web is the only tool offered and the one search was aimed at finding an adjudicating source, which is what the instructions permit searching for.",
  "parameters": 0.8,
  "parameters_reason": "The query found OFV, but \"official\" is doing the work a publication name would do better.",
  "call_count": 1.0,
  "call_count_reason": "One search against a ceiling of two, and the criteria named no source, so zero would not have been enough."
}
```

**6. The report.** Each score becomes its own column, with its reason printed under
`include_reasons=True`.

```
tool_selection    1.00
tool_parameters   0.80
tool_call_count   1.00
```

## What changes, file by file

### `backend/app/evals/trajectory.py` — new

```python
TRAJECTORY = "trajectory"
OUTPUT_TOOL = "final_result"
RESULT_CHARS = 600

def trajectory_events(messages: list[ModelMessage]) -> list[dict[str, Any]]
def render_trajectory(events: list[dict[str, Any]]) -> str

@contextmanager
def record_trajectory() -> Iterator[None]


class TrajectoryVerdict(BaseModel):
    tool_selection: float          # 0.0 - 1.0
    tool_selection_reason: str
    parameters: float
    parameters_reason: str
    call_count: float
    call_count_reason: str

@dataclass
class ToolTrajectoryJudge(Evaluator[object, object, object]):
    rubric: str
    model: str | None = None
    async def evaluate(self, ctx) -> dict[str, EvaluationReason]
```

`record_trajectory` wraps exactly one agent run. `capture_run_messages` keeps the
messages of the **first** run inside its context and reuses an outer context if one is
open, so a task that calls two agents must open one recorder per agent.

**This is a real limit, and the agentic evaluators do not share it.** They read the span
tree, which contains every tool call in the run including those made by nested sub-agents.
The critic makes one agent run, so the two agree there. An eval pointed at `outside_view`
or `inside_view` — which fan out over many runs — would get a complete count from
`MaxToolCalls` and a first-run-only transcript from the judge.

### `backend/app/evals/critic_eval.py` — new

Mirrors `decompose_eval.py`: `CASES`, mechanical evaluators, an `LLMJudge` on the output,
a `ToolTrajectoryJudge` on the run, and `--model` / `--judge-model` / `--budget` /
`--concurrency` flags.

| Evaluator | Kind | Scope | Asks |
|---|---|---|---|
| `Verdict` | mechanical, output | dataset | Does `is_resolvable` match the label? Did a `false` verdict come with a rewrite and a rationale? |
| `NamedASource` | mechanical, output | dataset | Is `suggested_resolution_source` non-empty? |
| `MaxToolCalls` | mechanical, spans | **case** | At most this many searches? `already_crisp` sets 1; the others take the prompt's 2 |
| `MaxModelRequests` | mechanical, spans | dataset | Within `BUDGETS["critic"].iterations` requests? |
| `LLMJudge(RUBRIC)` | second model | dataset | Is the rewrite actually adjudicable? |
| `ToolTrajectoryJudge(TOOL_RUBRIC)` | second model | dataset | Was the search worth making, aimed at the right thing, and not repeated? |

`MaxToolCalls` is per-case because the right ceiling is a property of the question, not of
the agent. It overlaps `ToolTrajectoryJudge` on purpose: the count is a fact and belongs in
an assertion; whether the count was right is a judgment.

`MaxModelRequests` is not a second runtime ceiling — the budget already enforces one. It is
the eval reporting *which case* ran the agent in circles, which `UsageLimitExceeded` cannot,
because that kills the run and returns a degraded critique with no trace of how close the
other cases came.

### The pydantic-ai 1.x → 2.x migration

The agentic evaluators live in `pydantic-evals` 2.x, which pins `pydantic-ai-slim` to its
exact version. Reaching them meant taking the major bump the old `<2` ceiling existed to
prevent. What actually broke, across 27 `pydantic_ai` import lines:

| Was | Is | Where |
|---|---|---|
| `Graph(nodes=[...], state_type=, run_end_type=)` | `GraphBuilder(state_type=, deps_type=, input_type=, output_type=)`, `.add(...)`, `.build()` | `superforecaster/update.py` |
| `graph.run(FirstNode(), state=, deps=)` returning a result with `.output` | `graph.run(inputs=FirstNode(), state=, deps=)` returning the output | `superforecaster/update.py` |
| `graph.mermaid_code(start_node=...)` | `graph.render()` | `superforecaster/update.py` |
| `async for node in run` yielding node instances | yields `EndMarker \| Sequence[GraphTask]`; the name is `task.node_id` | `tests/test_graph_update.py` |
| `result.usage()` | `result.usage` | `superforecaster/runner.py`, `tests/test_agent_budget.py` |
| `Agent(prepare_tools=fn)` | `Agent(capabilities=[Hooks(prepare_tools=fn)])` | `critic.py`, `inside_view.py`, `outside_view.py`, `tests/test_agent_budget.py` |
| `instrument_pydantic_ai(version=3)` | `version=5` — 2, 3, and 4 are deprecated | `app/observability.py` |

The node classes themselves did not change. `BaseNode`, `End`, and `GraphRunContext` keep
their shape, and the edges are still inferred from each node's `run` return type — so
`GuardUpdate -> VerifyLargeMove` still exists because `GuardUpdate.run` returns
`VerifyLargeMove | End[UpdateOutcome]`, and nowhere else. Only the assembly moved.

The mermaid render changed shape, not structure. A node with two successors now routes
through an explicit `<<choice>>` node, so `CheckResolved --> ApplyBayes` is drawn in two
hops.

### No production change from this spec

`run_critique` and every other agent entry point are untouched by the trajectory work.
`capture_run_messages` is a context variable, so the recorder reads the run from outside
it. The edits above are the version bump, not the feature.

## Cost

One extra judge call per case. The transcript is the largest input in the suite because it
carries tool returns, so returns are truncated to `RESULT_CHARS` characters. Three critic
cases with one or two searches each is roughly 15k judge input tokens for the whole
dataset — the trajectory judge is not what the eval budget goes on.

## What this does not do

- It does not use `TrajectoryMatch`. Pinning an expected ordered tool list would encode a
  shape these agents do not have — they fan out rather than following a fixed sequence.
  The judge reads the order and can say it was wrong; no assertion requires one.
- It does not use `ArgumentCorrectness`. It compares against an expected argument dict,
  and `search_web(query=...)` takes free text with no correct value to write down.
- It does not use `ToolCorrectness` yet. The critic has one tool, so "did it call
  `search_web`" is nearly vacuous. Add it when the evals reach `inside_view` (three tools)
  and `update` (two).
- It does not run under `uv run pytest`. Like `decompose_eval.py`, it calls the real model.

## Verified

- `uv run pytest` — 374 passed on 2.x.
- `MaxToolCalls` and `MaxModelRequests` against a `FunctionModel` stub that searches twice:
  the ceiling of 1 fails with *"2 tool call(s), budget=1"*, the ceiling of 2 passes, and
  `MaxModelRequests` counts 3 requests. Confirmed on `version=5` spans.
- `record_trajectory` on the same stub, returning the two calls, their arguments, their
  returns, and the reasoning between them — the part the span tree does not carry.
- Not verified: no case has been run against a real model, so no scores exist.
