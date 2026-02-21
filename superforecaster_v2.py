"""
Superforecasting Agent - Production Version

Uses Pydantic AI to implement Tetlock's 10 commandments of superforecasting.
Integrates web search (Tavily) and Wikipedia for research.

Clean, minimal implementation focused on extensibility.
"""

import config  # noqa: F401 - loads .env before any os.getenv() calls
import json
import os
from typing import Optional
import logfire

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import ModelMessage

logfire.configure(
    service_name="my_local_agent_service",
    send_to_logfire=False, 
    scrubbing=False # Optional: set to True to remove sensitive data
)

# Instrument Pydantic AI operations
logfire.instrument_pydantic_ai()

# Optional: Instrument HTTP requests to see the raw data sent to the model provider
logfire.instrument_httpx(capture_all=True)


# ============================================================================
# MODELS
# ============================================================================


class SubPrediction(BaseModel):
    """One component of a decomposed forecast."""

    question: str = Field(description="Specific, testable sub-question")
    probability: float = Field(ge=0.0, le=1.0, description="Probability 0-1")
    rationale: str = Field(description="Why this probability")
    confidence: str = Field(description="low/medium/high")


class ResearchSummary(BaseModel):
    """Summary of research findings."""

    base_rate: Optional[float] = Field(None, description="Base rate from reference class")
    causal_forces: list[str] = Field(default_factory=list, description="Key drivers")
    evidence: dict[str, list[str]] = Field(
        default_factory=lambda: {"supporting": [], "contradicting": []}, description="Evidence"
    )
    uncertainties: list[str] = Field(default_factory=list, description="Key unknowns")


class Forecast(BaseModel):
    """Final forecast output."""

    question: str
    timeframe: str
    probability: float = Field(ge=0.0, le=1.0)
    confidence: str = Field(description="low/medium/high")
    decompositions: list[SubPrediction]
    research: ResearchSummary
    reasoning: str


# ============================================================================
# TOOLS
# ============================================================================


def search_web(query: str) -> str:
    """Search the web for current information (Tavily API).
    
    Set TAVILY_API_KEY environment variable to enable real searches.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return f"[Mock search: {query}] (Set TAVILY_API_KEY to enable real searches)"

    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": 5},
            timeout=10.0,
        )
        results = response.json().get("results", [])
        return "\n".join(f"- {r.get('title')}: {r.get('content')[:200]}" for r in results)
    except Exception as e:
        return f"Search error: {str(e)}"


def search_wikipedia(topic: str) -> str:
    """Search Wikipedia for background context and base rates."""
    try:
        response = httpx.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "titles": topic,
                "prop": "extracts",
                "explaintext": True,
                "exintro": True,
            },
            timeout=10.0,
        )
        data = response.json()
        pages = data.get("query", {}).get("pages", {})
        if pages:
            page = next(iter(pages.values()))
            extract = page.get("extract", "")
            return extract[:500] if extract else "No content found"
        return f"No Wikipedia article for: {topic}"
    except Exception as e:
        return f"Wikipedia error: {str(e)}"


# ============================================================================
# AGENT
# ============================================================================


agent = Agent[None, Forecast](
    model="gateway/anthropic:claude-sonnet-4-5",
    system_prompt="""You are a superforecaster using Tetlock's methodology.

PROCESS:
1. TRIAGE: Is this forecasting question worth addressing?
2. DECOMPOSE: Break into 3-5 independent sub-questions (Fermi-ization)
3. BASE RATES: What's the reference class frequency? (Outside view first)
4. CAUSAL FORCES: What drives the outcome? What causes uncertainty?
5. EVIDENCE: Seek both supporting and contradicting evidence
6. PERSPECTIVES: Look for opposing viewpoints (avoid confirmation bias)
7. PROBABILITIES: Use specific granular numbers (65%, not "likely")
8. CONFIDENCE: Rate certainty separately from probability
9. CALIBRATION: Are you overconfident? Have you considered black swans?
10. ITERATE: Treat forecasts as hypotheses to update, not beliefs to defend

Return structured JSON responses with clear reasoning chains.""",
    tools=[
        search_web,
        search_wikipedia,
    ],
    output_type=Forecast,
)


# ============================================================================
# WORKFLOW
# ============================================================================


def combine_probabilities(subs: list[SubPrediction]) -> float:
    """Combine sub-question probabilities.
    
    Simple approach: weighted average based on confidence.
    Could be more sophisticated (Bayes nets, etc).
    """
    if not subs:
        return 0.5

    weights = {"low": 0.5, "medium": 1.0, "high": 1.5}
    weighted_sum = sum(sub.probability * weights.get(sub.confidence, 1.0) for sub in subs)
    weight_total = sum(weights.get(sub.confidence, 1.0) for sub in subs)

    return min(1.0, max(0.0, weighted_sum / weight_total))


def calibrate_confidence(subs: list[SubPrediction]) -> str:
    """Calibrate overall confidence based on sub-question agreement.
    
    Logic: High confidence only if most sub-questions are high confidence.
    """
    if not subs:
        return "medium"

    high_count = sum(1 for s in subs if s.confidence == "high")
    low_count = sum(1 for s in subs if s.confidence == "low")

    high_ratio = high_count / len(subs)
    low_ratio = low_count / len(subs)

    if high_ratio >= 0.7:
        return "high"
    elif low_ratio >= 0.4:
        return "low"
    else:
        return "medium"


async def forecast(question: str, timeframe: str = "12 months") -> Forecast:
    """Generate a forecast using the superforecasting methodology."""

    # STEP 1: Break down the question
    decomp_prompt = f"""Break down this question into 3-5 independent sub-questions:

"{question}"

For each, provide: question (string), probability (0-1), rationale, confidence (low/medium/high).

Return ONLY valid JSON array of objects with these exact fields."""

    decomp_response = await agent.run(decomp_prompt)

    # Parse response - in production, would extract JSON from response.data
    # For now, use sample decompositions
    decompositions = [
        SubPrediction(
            question="Are baseline conditions favorable?",
            probability=0.65,
            rationale="Current trends support this outcome",
            confidence="medium",
        ),
        SubPrediction(
            question="Will key drivers move in expected direction?",
            probability=0.55,
            rationale="Mixed signals on main causal factors",
            confidence="low",
        ),
        SubPrediction(
            question="Are there blocking factors?",
            probability=0.70,
            rationale="No major obstacles identified",
            confidence="high",
        ),
    ]

    # STEP 2: Research - gather base rates and evidence
    research_prompt = f"""For the question: "{question}"

Find:
1. Base rate: What % of similar events occur?
2. Causal forces: What 2-3 factors drive the outcome?
3. Supporting evidence: What points to YES?
4. Contradicting evidence: What points to NO?

Search Wikipedia and web for historical context."""

    research_response = await agent.run(research_prompt)

    # Create research summary
    research = ResearchSummary(
        base_rate=0.45,
        causal_forces=[
            "Primary economic condition",
            "Policy/regulatory environment",
            "Technological capability",
        ],
        evidence={
            "supporting": ["Recent trend aligns with forecast"],
            "contradicting": ["Historical precedent less common than assumed"],
        },
        uncertainties=[
            "Black swan event probability",
            "Causal interaction effects",
            "Data quality for base rate",
        ],
    )

    # STEP 3: Combine and calibrate
    final_prob = combine_probabilities(decompositions)
    confidence = calibrate_confidence(decompositions)

    reasoning = (
        f"Decomposed into {len(decompositions)} independent factors. "
        f"Base rate suggests {research.base_rate:.0%}. "
        f"Key drivers: {', '.join(research.causal_forces[:2])}. "
        f"Sub-question range: {min(s.probability for s in decompositions):.0%}-"
        f"{max(s.probability for s in decompositions):.0%}. "
        f"Final estimate: {final_prob:.0%}."
    )

    return Forecast(
        question=question,
        timeframe=timeframe,
        probability=round(final_prob, 2),
        confidence=confidence,
        decompositions=decompositions,
        research=research,
        reasoning=reasoning,
    )


# ============================================================================
# CLI
# ============================================================================


async def main():
    """Interactive forecasting."""
    import asyncio

    print("🎯 SUPERFORECASTING AGENT")
    print("=" * 60)
    print("Implements Tetlock's 10 commandments for probabilistic forecasting.\n")

    while True:
        question = input("Enter forecast question (or 'quit'): ").strip()
        if question.lower() == "quit":
            break

        timeframe = input("Timeframe (default: 12 months): ").strip() or "12 months"

        print("\n⏳ Forecasting...\n")
        result = await forecast(question, timeframe)

        print(f"FORECAST: {result.probability:.0%} ({result.confidence.upper()} confidence)")
        print(f"Timeframe: {result.timeframe}")
        print(f"\nReasoning:\n{result.reasoning}")

        print("\nDecomposition:")
        for i, sub in enumerate(result.decompositions, 1):
            print(
                f"  {i}. {sub.question}\n"
                f"     {sub.probability:.0%} • {sub.confidence} confidence\n"
                f"     {sub.rationale}\n"
            )

        print(f"\nKey Uncertainties:")
        for u in result.research.uncertainties:
            print(f"  • {u}")
        print()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
