"""The backtest corpus: 66 questions with known outcomes.

The questions live in `questions.json`, not here. They were 600 lines of dict literals in
a `.py` file that could not be imported — the fixtures called `datetime.date(...)` on a
module the file had imported as a *class*, so `import run_baseline` raised. Six hundred
lines of data pretending to be code, and broken code at that.

Each question carries `contamination_risk` 1-3: how likely a model is to have memorised
the answer rather than forecast it. That is the number that decides which model may see a
question at all — see `model_garden.pick_clean_model`, which refuses a model trained after
the question resolved.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

CORPUS = Path(__file__).resolve().parent / "questions.json"


def load_questions(path: Path = CORPUS) -> list[dict[str, Any]]:
    """The corpus, with date strings parsed back to `date`."""
    raw = json.loads(path.read_text())
    for q in raw:
        for field in ("date_created", "resolution_date"):
            if isinstance(q.get(field), str):
                q[field] = date.fromisoformat(q[field])
    return raw


def by_contamination_risk(risk: int, path: Path = CORPUS) -> list[dict[str, Any]]:
    """Questions at one risk level. `3` is the set most likely to be memorised."""
    return [q for q in load_questions(path) if q.get("contamination_risk") == risk]


if __name__ == "__main__":
    questions = load_questions()
    print(f"{len(questions)} questions")
    for risk in (1, 2, 3):
        n = len(by_contamination_risk(risk))
        print(f"  contamination risk {risk}: {n}")
