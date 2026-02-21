# Superforecasting Agent - Architecture

## Overview

A minimal, extensible forecasting agent implementing Tetlock's 10 commandments using Pydantic AI.

**Core principle**: Breaking hard questions into independent sub-questions dramatically improves accuracy.

## Files

### `superforecaster_v2.py` (Production Agent - 200 lines)
- Main agent implementation
- Clean data models (Pydantic)
- Core workflow: decompose → research → combine
- Two research tools: Wikipedia, web search

### `examples.py` (Testing & Examples)
- Demo workflows (single, batch, iterative, post-mortem)
- Forecast tracking system
- Calibration analysis tools
- Shows how to measure and improve accuracy

### `README.md` (User Guide)
- Installation and setup
- Usage examples
- Design rationale
- Extension patterns

## Data Flow

```
USER QUESTION
    ↓
[1] DECOMPOSE (Break Down)
    → 3-5 independent sub-questions
    → Probability + confidence for each
    ↓
[2] RESEARCH (Gather Evidence)
    → Wikipedia (base rates, context)
    → Web search (current conditions)
    → Identify causal forces
    ↓
[3] COMBINE (Synthesize)
    → Weighted average of sub-probabilities
    → Calibrate overall confidence
    → Generate reasoning chain
    ↓
FORECAST
    → Single percentage (0-100%)
    → Timeframe
    → Confidence level (low/medium/high)
    → Detailed decomposition & reasoning
```

## Key Classes

```python
SubPrediction
├─ question: str          # Testable sub-question
├─ probability: 0-1       # Specific granular number
├─ rationale: str         # Why this probability
└─ confidence: str        # low/medium/high

Forecast
├─ question: str
├─ timeframe: str
├─ probability: 0-1       # Final forecast (0-100%)
├─ confidence: str        # Overall confidence
├─ decompositions: [SubPrediction]
├─ research: ResearchSummary
└─ reasoning: str
```

## The 10 Commandments Implementation

| Commandment | Implementation |
|---|---|
| 1. **Triage** | Accept any question (pre-filter in production) |
| 2. **Break Down** | `decompose_prompt` → LLM suggests sub-questions |
| 3. **Balance Views** | Base rates from Wikipedia + current data from web |
| 4. **Balance Evidence** | Explicit supporting + contradicting evidence tracking |
| 5. **Causal Forces** | Research identifies key drivers |
| 6. **Degrees of Doubt** | Granular 0-1 probabilities (not "likely") |
| 7. **Balance Confidence** | Separate confidence from probability |
| 8. **Error Analysis** | `ForecastTracker.post_mortem()` tracks accuracy |
| 9. **Team Collab** | Multi-perspective research (built-in tool diversity) |
| 10. **Practice** | Iterative refinement + calibration tracking |

## Design Philosophy

### Simplicity
- Single file (no complex abstractions)
- Minimal dependencies (pydantic-ai, httpx)
- Clear control flow (no hidden magic)
- Easy to understand → easy to extend

### Extensibility
```python
# Add custom tool
def domain_expert_knowledge(topic: str) -> str:
    return search_custom_db(topic)

agent = Agent(
    tools=[search_web, search_wikipedia, domain_expert_knowledge]
)
```

### Accuracy
- Decomposition reduces overconfidence
- Base rates prevent "inside view" bias
- Confidence tracking enables calibration
- Iterative updates with new evidence

## How It Works

### Phase 1: Decomposition
```python
Question: "Will Bitcoin exceed $100k by end of 2026?"

Decomposed to:
├─ Will regulatory environment remain favorable? (65%)
├─ Will adoption trend continue? (70%)
└─ Will macro conditions support risk assets? (50%)

Combined: (0.65 + 0.70 + 0.50) / 3 = 61%
```

### Phase 2: Research
```python
Base Rate: "Historical precedent suggests 45% of similar gains occur"
Causal Forces: [
    "Regulatory clarity (key positive)",
    "Macro volatility (key risk)",
    "Institutional adoption (positive signal)"
]
```

### Phase 3: Confidence Calibration
```python
If most sub-questions: high confidence → overall high confidence
If mixed confidence → overall medium confidence
If many low confidence → overall low confidence
```

## Probability Combination

Simple weighted average (can be upgraded):

```python
weights = {
    "low": 0.5,      # Low confidence matters less
    "medium": 1.0,
    "high": 1.5      # High confidence matters more
}

final_probability = Σ(probability × weight) / Σ(weight)
```

For critical forecasts, consider Bayesian networks or expert aggregation.

## Calibration Tracking

### Brier Score
Mean squared error of probability forecasts:
- 0.0 = perfect
- 0.25 = random guessing (always 50%)
- Superforecasters: ~0.10-0.15

### Calibration by Bucket
If you forecast 60% on 100 events, ~60 should occur:
- If 75 occur: underconfident (need higher probabilities)
- If 45 occur: overconfident (need lower probabilities)

## Testing & Improvement

### Workflow
```
1. Generate forecast
2. Record question, probability, confidence
3. Outcome occurs
4. Compare: predicted vs actual
5. Analyze errors (was decomposition wrong? Base rate?)
6. Refine methodology for next time
```

### Example
```python
tracker = ForecastTracker()

# Record forecast
result = await forecast("Will X happen?")
tracker.add_forecast("Will X happen?", result)

# Later, mark outcome
tracker.update_outcome("Will X happen?", actual=True)

# Analyze calibration
report = tracker.calibration_report()
print(f"Brier Score: {report['brier_score']:.3f}")
```

## Production Considerations

### Scale
- Current: Handles ~5-10 forecasts/minute
- For bulk: Add async batching, rate limiting
- Cache base rates (don't re-query Wikipedia constantly)

### Quality
- Fine-tune base rate extraction from research
- Implement Bayesian decomposition combination
- Add expert elicitation interface
- Track forecaster-specific calibration

### Accuracy
- Post-mortems on all resolved forecasts
- Identify systematic biases (overconfident on tech?)
- Maintain reference class database
- Time-weight recent performance

## Next Steps

1. **Run examples**: `python examples.py single`
2. **Try a forecast**: `python superforecaster_v2.py`
3. **Extend**: Add domain-specific tools or templates
4. **Track**: Use `ForecastTracker` for 20+ forecasts
5. **Calibrate**: Analyze accuracy, refine methodology

---

Built on Tetlock's *Superforecasting* (2015) and Pydantic AI.
