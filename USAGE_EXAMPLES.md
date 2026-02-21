# Usage Examples

Copy-paste ready examples for common forecasting tasks.

## Example 1: Single Interactive Forecast

```python
import asyncio
from superforecaster_v2 import forecast

async def main():
    question = input("What would you like to forecast? ")
    timeframe = input("Timeframe (e.g., '6 months'): ") or "12 months"
    
    print("\n⏳ Generating forecast...\n")
    result = await forecast(question, timeframe)
    
    print(f"📊 FORECAST: {result.probability:.0%}")
    print(f"   Confidence: {result.confidence.upper()}")
    print(f"   Timeframe:  {result.timeframe}\n")
    
    print("💡 Reasoning:")
    print(f"   {result.reasoning}\n")
    
    print("📉 Decomposition:")
    for i, sub in enumerate(result.decompositions, 1):
        print(f"   {i}. {sub.question}")
        print(f"      {sub.probability:.0%} ({sub.confidence})")
        print(f"      {sub.rationale}\n")

asyncio.run(main())
```

Output:
```
What would you like to forecast? Will the Fed cut rates by June 2026?
Timeframe (e.g., '6 months'): 

⏳ Generating forecast...

📊 FORECAST: 72%
   Confidence: MEDIUM
   Timeframe:  12 months

💡 Reasoning:
   Decomposed into 3 independent factors...
```

## Example 2: Batch Forecasting

```python
import asyncio
from superforecaster_v2 import forecast
from examples import ForecastTracker

async def forecast_batch():
    questions = [
        "Will unemployment stay below 6% by end of 2026?",
        "Will inflation exceed 4% by June 2026?",
        "Will S&P 500 reach 7000 by end of 2026?",
    ]
    
    tracker = ForecastTracker()
    results = []
    
    for question in questions:
        print(f"⏳ Forecasting: {question}")
        result = await forecast(question)
        tracker.add_forecast(question, result)
        results.append((question, result.probability, result.confidence))
        print(f"   → {result.probability:.0%} ({result.confidence})\n")
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    for q, p, c in results:
        print(f"{p:.0%} | {c:6} | {q}")
    
    return tracker

asyncio.run(forecast_batch())
```

## Example 3: Track and Refine

```python
import asyncio
from superforecaster_v2 import forecast
from examples import ForecastTracker

async def track_and_refine():
    question = "Will Bitcoin exceed $100k by end of 2026?"
    tracker = ForecastTracker()
    
    # INITIAL FORECAST
    print("=" * 60)
    print("INITIAL FORECAST (Feb 21, 2026)")
    print("=" * 60)
    result1 = await forecast(question, timeframe="12 months")
    tracker.add_forecast(question, result1)
    print(f"Initial forecast: {result1.probability:.0%}")
    print(f"Confidence: {result1.confidence}")
    
    # ... time passes, new information emerges ...
    # 
    # UPDATED FORECAST
    print("\n" + "=" * 60)
    print("UPDATED FORECAST (June 2026)")
    print("=" * 60)
    print("New info: Major ETF approval announced")
    
    # Re-forecast with same question (would incorporate new context)
    result2 = await forecast(question, timeframe="6 months")
    tracker.add_forecast(question, result2, notes="After ETF approval news")
    print(f"Updated forecast: {result2.probability:.0%}")
    print(f"Confidence: {result2.confidence}")
    
    # Show change
    delta = (result2.probability - result1.probability) * 100
    direction = "↑" if delta > 0 else "↓"
    print(f"\nChange: {direction} {abs(delta):.0f}%")
    
    return tracker

asyncio.run(track_and_refine())
```

## Example 4: Analyze Accuracy

```python
from examples import ForecastTracker

def analyze_forecasts():
    tracker = ForecastTracker()
    
    # Load previous forecasts (from forecasts.jsonl)
    
    # Check calibration
    report = tracker.calibration_report()
    
    print("=" * 60)
    print("CALIBRATION REPORT")
    print("=" * 60)
    print(f"Total forecasts: {report['total_forecasts']}")
    print(f"Brier Score: {report.get('brier_score', 'N/A'):.3f}")
    print("(Lower is better; 0 = perfect, 0.25 = random)\n")
    
    print("Accuracy by Confidence Level:")
    conf = tracker.confidence_report()
    for level in ["high", "medium", "low"]:
        if level in conf:
            stats = conf[level]
            print(f"  {level.upper():6} confidence: "
                  f"{stats['accuracy']:.0%} correct "
                  f"(n={stats['count']})")
    
    print("\nCalibration by Probability Bucket:")
    for bucket, data in report['calibration_by_bucket'].items():
        if data['count'] > 0:
            print(f"  {bucket}: predicted {data['predicted_frequency']:.0%}, "
                  f"actual {data['actual_frequency']:.0%} "
                  f"(n={data['count']})")

analyze_forecasts()
```

## Example 5: Post-Mortem Analysis

```python
from examples import ForecastTracker, ForecastRecord

def postmortem_analysis():
    tracker = ForecastTracker()
    
    # Mark a forecast as resolved
    question = "Will Fed cut rates by June 2026?"
    tracker.update_outcome(
        question, 
        actual=True,
        notes="Fed cut 50bps in May"
    )
    
    # Find the forecast record
    for record in tracker.records:
        if record.question == question and record.actual_outcome is not None:
            error = record.calibration_error()
            print(f"Question: {record.question}")
            print(f"Forecast: {record.probability:.0%}")
            print(f"Outcome: {record.actual_outcome}")
            print(f"Error: {error:.1%}")
            
            if error < 0.15:
                print("✓ Well calibrated")
            elif error < 0.30:
                print("⚠ Slightly miscalibrated")
            else:
                print("✗ Poorly calibrated")

postmortem_analysis()
```

## Example 6: Custom Domain Forecast

```python
import asyncio
from superforecaster_v2 import Agent, Forecast

async def forecast_startup():
    """Forecast for startup investing domain"""
    
    question = (
        "Will this Series B startup reach $1B valuation "
        "within 5 years?"
    )
    
    # For startup domain, would add:
    # - Venture capital base rates (10-15% reach unicorn)
    # - Market size validation
    # - Team track record
    # - Product-market fit signals
    
    result = await forecast(question, timeframe="5 years")
    
    print(f"Unicorn forecast: {result.probability:.0%}")
    
    # Interpret
    if result.probability > 0.20:
        print("🎯 Strong potential unicorn candidate")
    elif result.probability > 0.10:
        print("📈 Reasonable unicorn potential")
    else:
        print("📊 Low probability unicorn, but solid growth company")
    
    return result

asyncio.run(forecast_startup())
```

## Example 7: Monitor Forecast Over Time

```python
import asyncio
from superforecaster_v2 import forecast
from examples import ForecastTracker
from datetime import datetime, timedelta

async def monitor_forecast():
    """Track same question over weeks/months as new info emerges"""
    
    question = "Will OpenAI reach $100B revenue by 2030?"
    tracker = ForecastTracker()
    
    # Week 1: Initial forecast
    print(f"Week 1 ({datetime.now().date()})")
    r1 = await forecast(question, "4 years")
    tracker.add_forecast(question, r1, notes="Initial estimate")
    print(f"  {r1.probability:.0%}")
    
    # Week 5: After major product launch
    print(f"\nWeek 5 ({(datetime.now() + timedelta(days=28)).date()})")
    print("  New: Major product launch announced")
    r2 = await forecast(question, "4 years")
    tracker.add_forecast(question, r2, notes="Post product launch")
    print(f"  {r2.probability:.0%}")
    
    # Week 13: Quarterly earnings
    print(f"\nWeek 13 ({(datetime.now() + timedelta(days=84)).date()})")
    print("  New: Quarterly earnings exceed expectations")
    r3 = await forecast(question, "4 years")
    tracker.add_forecast(question, r3, notes="Post earnings beat")
    print(f"  {r3.probability:.0%}")
    
    # Show trajectory
    probabilities = [r1.probability, r2.probability, r3.probability]
    print(f"\nTrajectory: {probabilities[0]:.0%} → {probabilities[1]:.0%} → {probabilities[2]:.0%}")
    
    return tracker

asyncio.run(monitor_forecast())
```

## Example 8: Team Disagreement Resolution

```python
import asyncio
from superforecasting_v2 import forecast

async def aggregate_views():
    """When team members disagree, use superforecasting"""
    
    question = "Will Bitcoin exceed $100k by end of 2026?"
    
    # Get independent forecasts
    print("Generating 3 independent forecasts (different random seeds):\n")
    
    forecasts = []
    for i in range(3):
        result = await forecast(question, "12 months")
        forecasts.append(result.probability)
        print(f"Forecast {i+1}: {result.probability:.0%}")
    
    # Aggregate
    from statistics import mean, stdev
    avg = mean(forecasts)
    try:
        std = stdev(forecasts)
    except:
        std = 0
    
    print(f"\n📊 AGGREGATE")
    print(f"   Average: {avg:.0%}")
    print(f"   Range: {min(forecasts):.0%} - {max(forecasts):.0%}")
    print(f"   Std Dev: {std:.1%}")
    
    # Interpretation
    if std < 0.05:
        print("   Agreement: High consensus")
    elif std < 0.10:
        print("   Agreement: Moderate consensus")
    else:
        print("   Agreement: Low consensus - decompose further")

asyncio.run(aggregate_views())
```

## Example 9: Sensitivity Analysis

```python
import asyncio
from superforecaster_v2 import forecast

async def sensitivity_analysis():
    """Test how forecast changes with different assumptions"""
    
    base_question = "Will inflation exceed 4% by June 2026?"
    
    print("BASE FORECAST:")
    base = await forecast(base_question)
    print(f"  {base.probability:.0%}\n")
    
    # Variant 1: Assume hawkish Fed
    print("SCENARIO: Hawkish Fed (higher rates)")
    variant1 = await forecast(
        base_question + " (assuming Fed stays hawkish)",
        "6 months"
    )
    print(f"  {variant1.probability:.0%}")
    
    # Variant 2: Assume dovish Fed
    print("\nSCENARIO: Dovish Fed (lower rates)")
    variant2 = await forecast(
        base_question + " (assuming Fed cuts rates)",
        "6 months"
    )
    print(f"  {variant2.probability:.0%}")
    
    # Show sensitivity
    print(f"\nSENSITIVITY:")
    print(f"  Base:      {base.probability:.0%}")
    print(f"  Hawkish:   {variant1.probability:.0%} "
          f"({variant1.probability - base.probability:+.0%})")
    print(f"  Dovish:    {variant2.probability:.0%} "
          f"({variant2.probability - base.probability:+.0%})")

asyncio.run(sensitivity_analysis())
```

## Example 10: Export Results

```python
import asyncio
import csv
from superforecaster_v2 import forecast
from datetime import datetime

async def export_forecasts():
    """Export forecasts to CSV for analysis"""
    
    questions = [
        "Will unemployment exceed 6%?",
        "Will inflation exceed 4%?",
        "Will Fed cut rates?",
    ]
    
    forecasts = []
    for q in questions:
        result = await forecast(q)
        forecasts.append({
            'question': q,
            'probability': f"{result.probability:.0%}",
            'confidence': result.confidence,
            'timeframe': result.timeframe,
            'timestamp': datetime.now().isoformat()
        })
    
    # Write to CSV
    with open('forecasts_export.csv', 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=forecasts[0].keys())
        writer.writeheader()
        writer.writerows(forecasts)
    
    print(f"✓ Exported {len(forecasts)} forecasts to forecasts_export.csv")

asyncio.run(export_forecasts())
```

---

All examples use the same core pattern:
1. Import forecast function
2. Call with question + timeframe
3. Get Forecast object with probability, confidence, decomposition
4. Use tracker to measure accuracy over time

See ARCHITECTURE.md for technical details.
