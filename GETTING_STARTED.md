# Getting Started with Superforecasting Agent

## 5-Minute Setup

### 1. Install Dependencies
```bash
uv sync
```

### 2. Set API Keys
update the .env from `.env.example`

### 3. Run Interactive Agent
```bash
python superforecaster_v2.py
```

Then enter a forecasting question:
```
Enter forecast question (or 'quit'): Will Bitcoin exceed $100k by end of 2026?
Timeframe (default: 12 months): 
```

Output:
```
⏳ Forecasting...

FORECAST: 65% (MEDIUM confidence)
Timeframe: 12 months

Reasoning:
Decomposed into 3 independent factors. Base rate suggests 45%. 
Key drivers: Regulatory environment, Market sentiment, Technical adoption. 
Final estimate accounts for uncertainty in these areas.

Decomposition:
  1. Will regulatory environment remain favorable?
     65% • medium confidence
     Regulatory clarity improving, but risks remain
  
  2. Will adoption trend continue?
     70% • high confidence
     Historical precedent shows sustained growth
  
  3. Will macro conditions support risk assets?
     50% • low confidence
     High uncertainty on interest rates and inflation
```

## Try the Examples

### Single Detailed Forecast
```bash
uv run examples.py single
```

Shows complete reasoning chain, decomposition, research findings.

### Batch Forecasts
```bash
uv run examples.py batch
```

Generate multiple forecasts and save to tracking file.

### Iterative Refinement
```bash
uv run examples.py refine
```

Demonstrates how to update forecasts with new information.

### Post-Mortem Analysis
```bash
uv run examples.py postmortem
```

Analyze forecasting accuracy and calibration.

## Core Workflow

### Generate a Forecast

```python
import asyncio
from superforecaster_v2 import forecast

async def main():
    result = await forecast(
        question="Will unemployment exceed 6% by end of 2026?",
        timeframe="12 months"
    )
    
    print(f"Forecast: {result.probability:.0%}")
    print(f"Confidence: {result.confidence}")
    print(f"Reasoning: {result.reasoning}")

asyncio.run(main())
```

### Track Forecasts Over Time

```python
from examples import ForecastTracker

tracker = ForecastTracker()

# Record a forecast
result = await forecast(question)
tracker.add_forecast(question, result)

# Later, when outcome is known
tracker.update_outcome(question, actual_outcome=True)

# Analyze calibration
report = tracker.calibration_report()
print(f"Brier Score: {report['brier_score']:.3f}")
```

## Understanding the Output

### Probability (0-100%)
- The agent's best estimate of the event occurring
- Based on decomposition, base rates, and evidence
- Granular: 65% not "likely"

### Confidence (low/medium/high)
- How certain the agent is in the probability
- Separate from probability itself
- Example: 70% confident in a 55% forecast

### Decomposition
- Shows the 3-5 sub-questions the agent broke down
- Each has its own probability and confidence
- Combined to get final forecast

### Research Notes
- Base rate: Historical frequency of similar events
- Causal forces: Key drivers identified
- Evidence: Supporting and contradicting points
- Uncertainties: Known unknowns

## Interpreting Decomposition

The agent breaks down complex questions to avoid overconfidence:

```
Hard Question: "Will AI cause unemployment > 20%?"

Decomposed to easier sub-questions:
├─ Will AI productivity gains exceed historical precedent?
├─ Will policy responses prevent structural unemployment?
└─ Will demand for new skills keep pace?
```

Each sub-question is easier to forecast independently, then combined.

## Improving Accuracy

### 1. Track 20+ Forecasts
- Single forecasts don't show patterns
- Need multiple to calibrate confidence

### 2. Do Post-Mortems
```python
# After outcome known:
actual = True  # Did predicted event happen?
forecast_prob = 0.65

# Did I predict correctly?
if (actual and forecast_prob > 0.5) or (not actual and forecast_prob < 0.5):
    print("✓ Correct direction")
else:
    print("✗ Wrong direction - why?")
```

### 3. Refine with New Evidence
```python
# Initial forecast: 60%
# New evidence emerges
# Re-run forecast with new information
result2 = await forecast(question)  # Now 70%

# Analyze what changed in the decomposition
```

### 4. Track Confidence Calibration
- If you forecast "high confidence" 10 times
- ~70% of those should be correct
- If only 40% are correct, you're overconfident

## Common Questions

### "What if I don't know the answer?"

That's fine! The agent uses:
1. **Base rates** - How often do similar events occur?
2. **Causal forces** - What drives this outcome?
3. **Evidence** - What information do we have?

You don't need domain expertise; the agent builds knowledge.

### "How do I know if the forecast is any good?"

Track outcomes:
- Generate forecast for question
- Record probability and confidence
- Outcome occurs (or doesn't)
- Compare: Did 65% forecasts occur 65% of the time?

This is called "calibration" - it's how superforecasters measure skill.

### "Can I use this for important decisions?"

Yes, but:
- Track multiple forecasts first (to validate accuracy)
- Use as one input, not sole decision-maker
- Combine with domain expertise
- Consider confidence level, not just probability

### "How can I improve the agent?"

See `ARCHITECTURE.md` for extension patterns:
- Add domain-specific tools
- Implement Bayesian decomposition
- Build base rate database
- Add expert elicitation interface

## Appendix: The 10 Commandments

Tetlock's framework that this agent implements:

1. **Triage** - Focus on answerable questions
2. **Break Down** - Fermi-ization into sub-problems
3. **Balance Perspectives** - Outside view (base rates) first
4. **Balance Evidence** - Avoid over/under-reacting to data
5. **Seek Causal Forces** - Identify underlying drivers
6. **Find Degrees of Doubt** - Use granular probabilities
7. **Balance Confidence** - Don't be reckless or timid
8. **Look for Error** - Post-mortems on mistakes
9. **Bring Out the Best** - Leverage diverse perspectives
10. **Master the Bicycle** - Practice, iterate, improve

## Next Steps

1. ✅ Install dependencies
2. ✅ Run interactive agent
3. ✅ Try example forecasts
4. ✅ Track 20+ forecasts
5. ✅ Analyze calibration
6. ✅ Refine methodology
7. ✅ Extend with custom tools

---

Questions? See README.md for technical details or ARCHITECTURE.md for design rationale.
