# Spec 10 — The name is sub-question

**Status: implemented** (August 2026). ADRs 56–57.

## Why

The decomposition splits a question into smaller **questions**. The code calls them
sub-claims. A claim is an assertion; these are questions with a probability attached.

The user interface has said "sub-question" from the start — `Break the question into 3–5
sub-questions`, the `Sub-question` column header, `All sub-questions need lenses before
base rates unlock`. Only the code, the wire format, and the ids disagree. A reader who
moves from the screen to the source has to learn that two names mean one thing.

The name is wrong in 695 places across 44 files.

**Outcome:** `sub_claim` and `sc1` appear nowhere in the running system.

## What changes

| Old | New |
|---|---|
| `Decomposition.sub_claims` | `sub_questions` |
| `sub_claim_id` — DB column, `RunStepOut` field, function arguments | `sub_question_id` |
| `sub_claim_ids` — on `ResearchedLens`, `Adjustment` | `sub_question_ids` |
| `SubClaimLenses`, `SubClaimLensesEdit` | `SubQuestionLenses`, `SubQuestionLensesEdit` |
| `SubClaimBaseRates`, `SubClaimAdjustments` | `SubQuestionBaseRates`, `SubQuestionAdjustments` |
| `subClaimById`, `subClaimRate` | `subQuestionById`, `subQuestionRate` |
| the ids `sc1`…`scN` | `sq1`…`sqN` |

`SubPrediction` keeps its name. It contains no variation of "claim".

`spec/implemented/*` and `spec/cancelled/*` keep the old name. They record what shipped
under the name it shipped with, which is the same reason ADR entries are superseded rather
than deleted.

`backend/run_checkpoints/*.json` are left alone — artifacts of the checkpoint mechanism
ADR 45 superseded. Nothing reads them.

### `agents/decompose.py` — `with_ids`

```python
def with_ids(d: Decomposition) -> Decomposition:
    """Stamp `sq1`…`sqN` onto the sub-questions."""
    return d.model_copy(update={
        "sub_questions": [s.model_copy(update={"id": f"sq{i}"})
                          for i, s in enumerate(d.sub_questions, 1)]
    })
```

## The database — migration 4

`sub_claim_id` is a column **and** part of `UNIQUE(run_id, stage, sub_claim_id, lens_name)`.
SQLite's `ALTER TABLE … RENAME COLUMN` rewrites the constraint with the column, so the
unique key needs no separate step.

The stored JSON is the hard part. Every `run_steps.payload_json` carries `"sub_claims"`,
`"sub_claim_ids"`, and `"id":"sc1"`; every `forecasts` row carries `decompositions[].id`.

**A textual replace over that JSON corrupts real prose.** A `"sc` → `"sq` rule also rewrites
`"bias":"scope_insensitivity"` and any `causal_forces: ["scarcity of chips"]` entry. There
is no safe purely-textual pattern, because the id values are ordinary JSON strings sitting
next to ordinary English. So the rewrite is structural, in Python.

### `MIGRATIONS` gains callables

```python
Step = str | Callable[[sqlite3.Connection], None]

MIGRATIONS: dict[int, tuple[Step, ...]] = {
    ...
    4: (
        "ALTER TABLE run_steps RENAME COLUMN sub_claim_id TO sub_question_id;",
        "UPDATE run_steps SET sub_question_id = 'sq' || substr(sub_question_id, 3) "
        "WHERE sub_question_id GLOB 'sc[0-9]*';",
        _rewrite_sub_claim_payloads,
    ),
}
```

`_migrate` calls a callable step instead of `conn.execute`. `_MIGRATION_NO_OPS` already
carries `"no such column"` — exactly what the `RENAME COLUMN` raises on a fresh database
whose `CREATE TABLE` block already wrote `sub_question_id` — so the step is a no-op there,
which is what that mechanism is for.

`_rewrite_sub_claim_payloads(conn)` walks each parsed document:

- renames the keys `sub_claims`, `sub_claim_ids`, `sub_claim_id`
- rewrites a string matching `^sc(\d+)$` to `sq\1`, **only** as the `id` of a
  `sub_questions` entry or inside a `sub_question_ids` list

It runs over `run_steps.payload_json` and the `forecasts` row holding a `Forecast`.

`SCHEMA_VERSION` goes to 4.

## Data lineage — one migrated step row

```json
{"before": {"table": "run_steps",
            "sub_claim_id": "sc2",
            "stage": "base_rates",
            "payload_json": "{\"lens\":{\"name\":\"FOMC cutting cycles\",\"sub_claim_ids\":[\"sc2\"]},\"disagreement\":\"…\"}"}}
```

```
_migrate -> version 4
  -> ALTER TABLE run_steps RENAME COLUMN sub_claim_id TO sub_question_id
  -> UPDATE run_steps SET sub_question_id = 'sq2' WHERE …           [writes: run_steps]
  -> _rewrite_sub_claim_payloads(conn)                    [reads+writes: run_steps, forecasts]
```

```json
{"after": {"table": "run_steps",
           "sub_question_id": "sq2",
           "stage": "base_rates",
           "payload_json": "{\"lens\":{\"name\":\"FOMC cutting cycles\",\"sub_question_ids\":[\"sq2\"]},\"disagreement\":\"…\"}"}}
```

```
GET /runs/{id}
  -> machine.detail(run_id) -> RunStepOut{"sub_question_id": "sq2", …}
  -> RunView: subQuestionById["sq2"] -> "Sub-question 2 — …"
```

## Tests

- A version-3 database holding one run with `sc1`/`sc2` payloads upgrades to 4, and every
  step still parses into its stage model.
- `_rewrite_sub_claim_payloads` leaves `bias: "scope_insensitivity"` and
  `causal_forces: ["scarcity of chips"]` untouched. This is the regression a textual
  replace would cause, so it is the test that matters.
- A fresh database reaches version 4 with no error — the `RENAME COLUMN` no-op path.
- `with_ids` stamps `sq1`…`sqN`.
