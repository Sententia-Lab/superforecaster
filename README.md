# Superforecasting Agent

A minimalist Python agent that can forecast future events.

Implements Tetlock's "10 Commandments of Superforecasting" using Pydantic AI.

## Quick Start

### Installation

```bash
uv sync
```

### Setup Environment

copy the `.env.example` file and add API keys.

### Run

```bash
uv run superforecaster_v2.py
```

## Architecture

The agent implements Tetlock's 10 commandments in a clean workflow:

```
1. TRIAGE           → Accept forecasting question
2. BREAK DOWN       → Decompose into sub-questions (Fermi-ization)
3. BASE RATES       → Establish reference class frequency (outside view)
4. CAUSAL FORCES    → Identify key drivers and mechanisms
5. EVIDENCE         → Gather supporting & contradicting data
6. PERSPECTIVES     → Seek opposing viewpoints
7. PROBABILITIES    → Use granular numbers (65% not "likely")
8. CONFIDENCE       → Rate certainty separately from probability
9. CALIBRATION      → Check for overconfidence/blind spots
10. ITERATE         → Treat forecasts as updateable hypotheses
```

## Key Design Decisions

### Simplicity First
- Single Python file (v2) with ~200 lines of core logic
- Pydantic models for clean data passing
- Direct Pydantic AI integration (no custom wrappers)

### Decomposition
The core insight of superforecasting is that breaking hard questions into independent sub-questions dramatically improves accuracy. The agent:

1. **Identifies sub-questions** using the LLM
2. **Assigns probabilities** to each (with confidence levels)
3. **Combines them** using weighted averaging
4. **Calibrates confidence** based on agreement

```python
decompositions = [
    SubPrediction(
        question="Are baseline conditions favorable?",
        probability=0.65,
        rationale="Current trends support outcome",
        confidence="medium"
    ),
    # ... more sub-predictions
]

final_probability = combine_probabilities(decompositions)
```

### Research Integration

Two research tools follow the outside-view-first principle:

1. **Wikipedia** - Historical context, base rates, reference classes
2. **Web Search** (Tavily) - Current conditions, recent data

The agent queries these for:
- Base rate (how often do similar events occur?)
- Causal forces (what drives the outcome?)
- Evidence (supporting & contradicting)

### Probability Calibration

```python
def combine_probabilities(subs: list[SubPrediction]) -> float:
    """Weighted by confidence - high confidence estimates matter more"""
    weights = {"low": 0.5, "medium": 1.0, "high": 1.5}
    weighted_sum = sum(sub.probability * weights[sub.confidence] for sub in subs)
    return weighted_sum / sum(weights[sub.confidence] for sub in subs)
```

Confidence is calibrated based on sub-question agreement:
- High confidence: 70%+ sub-questions are high confidence
- Low confidence: 40%+ sub-questions are low confidence
- Medium: everything else

## Example Usage

```python
import asyncio
from superforecaster_v2 import forecast

# Generate a forecast
result = await forecast(
    question="Will Bitcoin exceed $100k by end of 2026?",
    timeframe="12 months"
)

print(f"Forecast: {result.probability:.0%}")
print(f"Confidence: {result.confidence}")
print(f"Reasoning: {result.reasoning}")
```

Output:
```
Forecast: 65%
Confidence: medium
Reasoning: Decomposed into 3 independent factors. Base rate suggests 45%. 
Key drivers: Regulatory environment, Market sentiment, Technical adoption. 
Final estimate accounts for uncertainty in these areas.
```

## Extending the Agent

### Add New Tools

```python
def my_custom_tool(param: str) -> str:
    """Custom research tool"""
    return "research result"

agent = Agent(
    model="claude-opus-4-5-20251101",
    tools=[search_web, search_wikipedia, my_custom_tool],
    # ...
)
```

### Improve Decomposition

The current approach uses the LLM to suggest sub-questions. For critical forecasts, you could:

```python
# Add domain-specific decomposition patterns
def decompose_geopolitical_event(question: str) -> list[SubPrediction]:
    """Template for geopolitical questions"""
    return [
        "Will trigger event occur?",
        "Will actors respond as expected?",
        "Will intended effects materialize?",
        "Will unintended consequences dominate?",
    ]
```

### Implement Bayesian Combination

Instead of weighted averaging, use proper Bayesian combination:

```python
def bayes_combine(subs: list[SubPrediction]) -> float:
    """Proper Bayesian update approach"""
    # This would require more sophisticated probability theory
    # and independence assumptions
    pass
```

## Implementation Notes

### Why Pydantic AI?

- Native support for structured outputs (our `Forecast` model)
- Clean tool integration with type safety
- Direct Claude API access
- Minimal boilerplate

### Why Single File?

- Easy to read and understand the full pipeline
- Simple to extend with custom logic
- Plays well with version control
- Natural fit for an agent that fits in one mental model

### Base Rate Finding

The agent searches Wikipedia for historical context. Better implementations would:

- Maintain a database of reference class frequencies
- Apply Laplace smoothing to avoid overconfident estimates
- Track base rate accuracy over time

## Testing

Superforecasting improves through error analysis. Track your forecasts:

```python
# After the event occurs:
actual_outcome = True  # Did the predicted event happen?
forecast_probability = 0.65

# Check calibration
# If you forecast 65% on 100 events, ~65 should occur
# If 80 occur, you're underconfident; if 50 occur, overconfident
```

## References

- Tetlock, P. E., & Gardner, D. (2015). *Superforecasting*
- Core principle: Breaking down hard questions → probabilistic thinking → iterative improvement
- The framework trades overconfident narratives for calibrated uncertainty
- Prompt used to create base structure:
```
The book, Superforecasters outlines methodologies for how to forecast future events. Can you use your knowledge of the book and the 10 commandments outlined here:

Core Principles and Techniques
The framework is characterized by a disciplined, "Bayesian" approach to updating beliefs based on new information. 
Fermi-ization (Decomposition): Breaking down seemingly intractable questions into smaller, manageable sub-problems, a technique inspired by physicist Enrico Fermi.
Outside-View First (Base Rates): Instead of focusing on the specifics of a unique event, superforecasters start by analyzing the base rates—how often does this type of event occur in similar situations?
Inside-View Second (Case Specifics): After establishing the base rate, they adjust their prediction based on the unique factors of the current case.
Probabilistic Thinking: Moving away from binary "yes/no" predictions to, for example, a 65% or 70% probability. They use granular, specific numbers (e.g., distinguishing 60% from 65%).
Belief Updating: Treating forecasts as hypotheses to be tested, not treasures to be guarded. When new information arrives, they update their probabilities incrementally, avoiding the temptation to stick to earlier predictions (belief perseverance).
Dragonfly Eye Perspective: Actively seeking out diverse, opposing, and conflicting viewpoints to avoid confirmation bias. 

The 10 Commandments of Superforecasting
Tetlock summarized the methodology in "10 Commandments" for aspiring forecasters: 
Triage: Focus on problems where effort has the highest payoff (avoiding too-easy or too-hard problems).
Break Down: Decompose questions (Fermi-ize).
Balance Perspectives: Mix inside and outside views.
Balance Evidence: Avoid under- or overreacting to new data.
Seek Causal Forces: Look for underlying, clashing drivers.
Find Degrees of Doubt: Use granular probabilities.
Balance Confidence: Be neither a timid waffler nor a reckless gambler.
Look for Error in Mistakes: Perform post-mortems on failed predictions.
Bring Out the Best in Others: Leverage team collaboration.
Master the "Bicycle": Practice the above, as it is a skill learned by doing. 

Key Traits and Mindset
Superforecasters are not necessarily genius-level intellectuals, but rather possess specific, cultivated habits: 

Active Open-mindedness: Willingness to reconsider beliefs.
Intellectual Humility: Acknowledging that the future is uncertain.
Analytical & Numerate: Comfortable with data and statistics.
"Perpetual Beta": A commitment to self-improvement and learning from mistakes.
Grit: The tenacity to stick with a long-term, complex problem. 

Implementation and Application
Teamwork: Superforecasters often work in teams, which helps reduce individual bias, though the best teams are not hierarchical.
AI Integration: The framework is now being applied to AI, with "Superforecasting LLMs" mimicking these techniques to improve AI-driven predictions, showing up to 41% accuracy improvements in certain scenarios.
Commercial/Government Use: Organizations like Good Judgment Inc. use this framework for forecasting geopolitical, financial, and public health risks, such as the trajectory of COVID-19 or Fed interest rate decisions.
I want to create an AI agent that uses, for starters, web search with Tavily, and wikipedia to come up with forecasts based on a question that a user may have. It should spit out a timeframe and a single percentage score. 

A few things the agent should be able to do:
1. it should use as many/all the principled and mindsets outlined above in a structured way. Core to this method is breaking down large predictions into smaller predictions.
2. It should follow the methodology above as closely as possible.

Create the agent using pydnatic AI gateway and pydantic agents framework in a single python file with all the tools necessary.
```

## License

MIT
