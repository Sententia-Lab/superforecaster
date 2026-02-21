"""
Examples and Testing Patterns for Superforecasting Agent

Shows how to use the agent on real forecasting questions and track accuracy.
"""

import config  # noqa: F401 - loads .env before any os.getenv() calls
import asyncio
from datetime import datetime
from typing import Optional
from dataclasses import dataclass
import json

from superforecaster_v2 import forecast, Forecast


# ============================================================================
# EXAMPLE QUESTIONS
# ============================================================================

EXAMPLE_QUESTIONS = {
    "geopolitical": [
        "Will the US unemployment rate exceed 6% by end of 2026?",
        "Will a new peace agreement be signed in the Middle East by end of 2026?",
        "Will China implement major new AI regulations by mid-2026?",
    ],
    "technology": [
        "Will GPT-6 (or equivalent) be released by end of 2025?",
        "Will Tesla's market cap exceed $1 trillion by end of 2026?",
        "Will quantum computers demonstrate supremacy on a practical problem by 2026?",
    ],
    "economic": [
        "Will the S&P 500 close above 6500 on December 31, 2026?",
        "Will US inflation exceed 5% (YoY) by June 2026?",
        "Will Bitcoin's price exceed $100,000 by end of 2026?",
    ],
    "science": [
        "Will a breakthrough in fusion energy be announced by major lab by 2026?",
        "Will mRNA cancer vaccines enter Phase 3 trials by 2026?",
        "Will a new element be discovered by 2026?",
    ],
}


# ============================================================================
# TRACKING & EVALUATION
# ============================================================================


@dataclass
class ForecastRecord:
    """Track a forecast for later evaluation."""

    question: str
    forecast_date: str
    probability: float
    timeframe: str
    confidence: str
    actual_outcome: Optional[bool] = None
    outcome_date: Optional[str] = None
    notes: str = ""

    def to_dict(self):
        return {
            "question": self.question,
            "forecast_date": self.forecast_date,
            "probability": self.probability,
            "timeframe": self.timeframe,
            "confidence": self.confidence,
            "actual_outcome": self.actual_outcome,
            "outcome_date": self.outcome_date,
            "notes": self.notes,
        }

    def calibration_error(self) -> Optional[float]:
        """Return error if outcome known: |probability - actual|"""
        if self.actual_outcome is None:
            return None
        actual_prob = 1.0 if self.actual_outcome else 0.0
        return abs(self.probability - actual_prob)


class ForecastTracker:
    """Track forecasts over time to measure accuracy and calibration."""

    def __init__(self, storage_file: str = "forecasts.jsonl"):
        self.storage_file = storage_file
        self.records: list[ForecastRecord] = []
        self._load_records()

    def _load_records(self):
        """Load existing records from file."""
        try:
            with open(self.storage_file, "r") as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        self.records.append(ForecastRecord(**data))
        except FileNotFoundError:
            pass

    def add_forecast(
        self,
        question: str,
        result: Forecast,
        notes: str = "",
    ) -> ForecastRecord:
        """Record a new forecast."""
        record = ForecastRecord(
            question=question,
            forecast_date=datetime.now().isoformat(),
            probability=result.probability,
            timeframe=result.timeframe,
            confidence=result.confidence,
            notes=notes,
        )
        self.records.append(record)
        self._save_record(record)
        return record

    def _save_record(self, record: ForecastRecord):
        """Append record to storage file."""
        with open(self.storage_file, "a") as f:
            f.write(json.dumps(record.to_dict()) + "\n")

    def update_outcome(self, question: str, actual: bool, notes: str = ""):
        """Mark a forecast as resolved."""
        for record in self.records:
            if record.question == question:
                record.actual_outcome = actual
                record.outcome_date = datetime.now().isoformat()
                record.notes = notes
                self._save_record(record)
                return

    def calibration_report(self) -> dict:
        """Analyze calibration across all resolved forecasts."""
        resolved = [r for r in self.records if r.actual_outcome is not None]

        if not resolved:
            return {"status": "no resolved forecasts"}

        # Group by probability bucket
        buckets = {
            "0-20%": [r for r in resolved if 0.0 <= r.probability < 0.2],
            "20-40%": [r for r in resolved if 0.2 <= r.probability < 0.4],
            "40-60%": [r for r in resolved if 0.4 <= r.probability < 0.6],
            "60-80%": [r for r in resolved if 0.6 <= r.probability < 0.8],
            "80-100%": [r for r in resolved if 0.8 <= r.probability <= 1.0],
        }

        report = {
            "total_forecasts": len(resolved),
            "calibration_by_bucket": {},
        }

        for bucket_name, records in buckets.items():
            if records:
                bucket_min = float(bucket_name.split("-")[0].strip("%")) / 100
                correct = sum(1 for r in records if r.actual_outcome)
                report["calibration_by_bucket"][bucket_name] = {
                    "count": len(records),
                    "predicted_frequency": bucket_min,
                    "actual_frequency": correct / len(records),
                    "forecast_errors": [r.calibration_error() for r in records],
                }

        # Brier score (mean squared error)
        errors = [r.calibration_error() for r in resolved]
        report["brier_score"] = sum(e**2 for e in errors) / len(errors)

        return report

    def confidence_report(self) -> dict:
        """Analyze how often high-confidence forecasts are correct."""
        resolved = [r for r in self.records if r.actual_outcome is not None]

        report = {}
        for confidence_level in ["low", "medium", "high"]:
            matching = [r for r in resolved if r.confidence == confidence_level]
            if matching:
                correct = sum(1 for r in matching if r.actual_outcome)
                report[confidence_level] = {
                    "count": len(matching),
                    "accuracy": correct / len(matching),
                    "average_probability": sum(r.probability for r in matching) / len(matching),
                }

        return report


# ============================================================================
# DEMONSTRATION
# ============================================================================


async def demo_single_forecast():
    """Demonstrate a single forecast."""
    print("=" * 70)
    print("SINGLE FORECAST DEMO")
    print("=" * 70 + "\n")

    question = "Will the S&P 500 close above 6500 on December 31, 2026?"
    print(f"Question: {question}\n")

    result = await forecast(question, timeframe="12 months")

    print(f"📊 FORECAST RESULT")
    print(f"   Probability: {result.probability:.0%}")
    print(f"   Confidence:  {result.confidence.upper()}")
    print(f"   Timeframe:   {result.timeframe}\n")

    print(f"💡 REASONING")
    print(f"   {result.reasoning}\n")

    print(f"📉 DECOMPOSITION")
    for i, sub in enumerate(result.decompositions, 1):
        print(
            f"   {i}. {sub.question}\n"
            f"      → {sub.probability:.0%} ({sub.confidence})\n"
            f"      {sub.rationale}\n"
        )

    print(f"🔍 RESEARCH NOTES")
    print(f"   Base Rate: {result.research.base_rate:.0%}")
    print(f"   Causal Factors:")
    for force in result.research.causal_forces:
        print(f"      • {force}")
    print(f"\n   Key Uncertainties:")
    for unc in result.research.uncertainties:
        print(f"      • {unc}")

    return result


async def demo_batch_forecasts():
    """Generate multiple forecasts and show aggregated insights."""
    print("\n" + "=" * 70)
    print("BATCH FORECAST DEMO")
    print("=" * 70 + "\n")

    tracker = ForecastTracker()
    questions = EXAMPLE_QUESTIONS["economic"][:2]  # Use first 2 economic questions

    for question in questions:
        print(f"⏳ Forecasting: {question}")
        result = await forecast(question)
        tracker.add_forecast(question, result)
        print(f"   → {result.probability:.0%} ({result.confidence})\n")

    # Show summary
    print(f"\nRecords tracked: {len(tracker.records)}")
    print(f"Storage file: {tracker.storage_file}")


async def demo_iterative_refinement():
    """Show how to refine a forecast with additional information."""
    print("\n" + "=" * 70)
    print("ITERATIVE REFINEMENT DEMO")
    print("=" * 70 + "\n")

    question = "Will Bitcoin exceed $100,000 by end of 2026?"

    # Initial forecast
    print("INITIAL FORECAST")
    result1 = await forecast(question, timeframe="12 months")
    print(f"   {result1.probability:.0%} confidence: {result1.confidence}\n")

    # In real use, new information would arrive here
    # For demo, we'd refine based on new data
    print("HYPOTHESIS: Major institution adoption announced")
    print("   This would shift probability based on new evidence\n")

    print("TO REFINE:")
    print("   1. Re-run forecast with updated information")
    print("   2. Compare decompositions to previous forecast")
    print("   3. Identify which sub-questions changed most")
    print("   4. Track whether changes were justified\n")


async def demo_post_mortem():
    """Show how to analyze forecast accuracy after outcomes are known."""
    print("\n" + "=" * 70)
    print("POST-MORTEM ANALYSIS DEMO")
    print("=" * 70 + "\n")

    tracker = ForecastTracker()

    # Add some sample resolved forecasts
    print("Sample resolved forecasts:")
    sample_records = [
        ForecastRecord(
            question="Will event A occur?",
            forecast_date="2025-01-01",
            probability=0.75,
            timeframe="6 months",
            confidence="high",
            actual_outcome=True,
        ),
        ForecastRecord(
            question="Will event B occur?",
            forecast_date="2025-01-01",
            probability=0.40,
            timeframe="6 months",
            confidence="medium",
            actual_outcome=False,
        ),
    ]

    for record in sample_records:
        tracker.records.append(record)
        tracker._save_record(record)
        outcome = "✓" if record.actual_outcome else "✗"
        error = record.calibration_error()
        print(f"   {outcome} {record.probability:.0%} → {record.actual_outcome} (error: {error:.1%})")

    print("\nCALIBRATION REPORT:")
    cal_report = tracker.calibration_report()
    print(f"   Brier Score: {cal_report.get('brier_score', 'N/A'):.3f}")
    print(f"   (Lower is better; 0 = perfect, 0.25 = random guessing)")

    print("\nCONFIDENCE ANALYSIS:")
    conf_report = tracker.confidence_report()
    for level, stats in conf_report.items():
        if stats:
            print(
                f"   {level.upper()}: {stats['accuracy']:.0%} correct "
                f"(avg forecast: {stats['average_probability']:.0%})"
            )


async def main():
    """Run demonstrations."""
    import sys

    if len(sys.argv) > 1:
        demo = sys.argv[1]
    else:
        print("Usage: python examples.py [demo_name]")
        print("\nAvailable demos:")
        print("  single      - Generate one forecast with detailed breakdown")
        print("  batch       - Generate multiple forecasts")
        print("  refine      - Show iterative refinement process")
        print("  postmortem  - Analyze accuracy of resolved forecasts")
        return

    if demo == "single":
        await demo_single_forecast()
    elif demo == "batch":
        await demo_batch_forecasts()
    elif demo == "refine":
        await demo_iterative_refinement()
    elif demo == "postmortem":
        await demo_post_mortem()


if __name__ == "__main__":
    asyncio.run(main())
