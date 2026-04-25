# Current State

What exists in the codebase today, what works, and what's broken.

---

## Repository Layout

```
superforecaster/
├── superforecaster_v2.py   # Main agent (single file, ~200 lines)
├── pyproject.toml          # uv project config
├── .env.example            # API key template
├── README.md               # Public-facing docs
└── spec/
    ├── CURRENT_STATE.md            # This file
    ├── TECHNICAL_DIRECTION.md      # Architecture decisions + desired features
    ├── SPEC.md                     # Implementation specs (4 phases)
    └── superforecasting_methodology.md   # Tetlock methodology reference
```

No package structure yet. Everything lives in `superforecaster_v2.py`.

---

## Existing Code

### Data Models (`superforecaster_v2.py`)

```python
SubPrediction
├── question: str       # Testable sub-question
├── probability: float  # 0.0–1.0
├── rationale: str
└── confidence: str     # "low" | "medium" | "high"

ResearchSummary
├── base_rate: float | None
├── causal_forces: list[str]
├── evidence: dict        # { "supporting": [...], "contradicting": [...] }
└── uncertainties: list[str]

Forecast
├── question: str
├── timeframe: str
├── probability: float    # 0.0–1.0
├── confidence: str       # "low" | "medium" | "high"
├── decompositions: list[SubPrediction]
├── research: ResearchSummary
└── reasoning: str
```

### Tools

| Tool | Source | Status |
|---|---|---|
| `search_web(query)` | Tavily API | Works if `TAVILY_API_KEY` is set; returns mock string otherwise |
| `search_wikipedia(topic)` | Wikipedia API | Always available |

### Core Functions

| Function | What it does |
|---|---|
| `combine_probabilities(subs)` | Weighted average of sub-probabilities; weights by confidence level (low=0.5, medium=1.0, high=1.5) |
| `calibrate_confidence(subs)` | Returns "high" if 70%+ subs are high-confidence; "low" if 40%+ are low-confidence; "medium" otherwise |
| `forecast(question, timeframe)` | Orchestrates the full workflow; returns a `Forecast` object |

### Pydantic AI Agent

```python
agent = Agent[None, Forecast](
    model="gateway/anthropic:claude-sonnet-4-5",   # NOTE: outdated model
    output_type=Forecast,
    tools=[search_web, search_wikipedia],
    system_prompt="..."   # Tetlock's 10 commandments as instructions
)
```

### CLI

```
uv run superforecaster_v2.py
```

Interactive loop: prompts for question + timeframe, prints probability, confidence, reasoning, decompositions, uncertainties.

---

## What Actually Works

- CLI runs and produces output
- `search_wikipedia` fetches real Wikipedia content
- `search_web` gracefully degrades to mock if no Tavily key
- `combine_probabilities` and `calibrate_confidence` are correctly implemented
- Pydantic model validation is wired up correctly
- Logfire instrumentation is configured

---

## Known Issues / Broken Behavior

**Critical: LLM output is ignored.** The `forecast()` function calls the agent twice (decompose prompt + research prompt) but then discards both responses and substitutes hardcoded mock data:

```python
# This runs but the result is never used:
decomp_response = await agent.run(decomp_prompt)

# Then this hardcoded list is used instead:
decompositions = [
    SubPrediction(question="Are baseline conditions favorable?", probability=0.65, ...)
    ...
]
```

Same pattern for the research step — the LLM searches Wikipedia and the web, but the `ResearchSummary` returned is a hardcoded placeholder.

**Result:** Every forecast returns ~62% probability with the same three generic sub-questions regardless of the actual question asked.

**Secondary issues:**
- Model is `claude-sonnet-4-5` (outdated; should be `claude-sonnet-4-6`)
- No persistence — forecasts are printed and discarded
- No tests
- `ForecastTracker` and calibration tools referenced in docs do not exist in the codebase (they are documented in examples but `examples.py` does not exist)

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `pydantic-ai` | ≥0.1.0 | Agent framework |
| `pydantic` | ≥2.0.0 | Data models |
| `anthropic` | ≥0.7.0 | Claude API client |
| `httpx` | ≥0.24.0 | HTTP calls for tools |
| `logfire` | ≥4.25.0 | Observability |
| `python-dotenv` | ≥1.0.0 | `.env` loading |

Runtime manager: `uv`. Python ≥3.12 required.

---

## Environment Variables

```
PYDANTIC_AI_GATEWAY_API_KEY=   # Required: Pydantic AI gateway key
TAVILY_API_KEY=                # Optional: enables real web search
LOGFIRE_TOKEN=                 # Optional: enables Logfire traces
```

---

## Deployment Assets

None. This is currently a local CLI tool only. No server, no database, no hosted deployment.
