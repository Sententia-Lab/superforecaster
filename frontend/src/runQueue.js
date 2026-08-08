// Which step runs next. Pure, so it stays testable the day a runner exists.
//
// Mirrors `db.STAGE_ORDER` and the gate in `machine.gate_offender`, the same way
// `derive.js` mirrors `checks.py`. The server enforces both regardless; checking here
// means a blocked stage stops the queue instead of producing a 409.

export const STAGE_ORDER = [
  "decompose",
  "lenses",
  "base_rates",
  "inside_view",
  "synthesis",
];

const runnable = (s) => s.status === "pending" || s.status === "error";

/**
 * A stage with zero rows is NOT done — its rows have not been created yet.
 *
 * Same rule as `machine._all_complete`. Reading "no rows" as "finished" would let the
 * queue skip a whole stage that simply has not fanned out yet.
 */
const stageDone = (steps, stage) => {
  const rows = steps.filter((s) => s.stage === stage);
  return rows.length > 0 && rows.every((s) => s.status === "complete");
};

/**
 * The next step that may legally run, or null when there is nothing to do.
 *
 * `scope` is "all" or a single stage name. A step in `error` counts as runnable, which
 * is what lets a second Run All resume from a failure rather than stalling on it.
 */
export function nextRunnable(run, scope) {
  const steps = run?.steps || [];
  for (const [i, stage] of STAGE_ORDER.entries()) {
    if (scope !== "all" && stage !== scope) continue;
    const next = steps.find((s) => s.stage === stage && runnable(s));
    if (!next) continue;
    // The gate: every earlier stage must be complete. If it is not satisfied here it is
    // not satisfied for anything later either, so stop rather than skipping ahead.
    if (!STAGE_ORDER.slice(0, i).every((p) => stageDone(steps, p))) return null;
    return next;
  }
  return null;
}

/** True when a stage has work left and its gate is satisfied — the Run Section test. */
export function sectionRunnable(run, stage) {
  return nextRunnable(run, stage) !== null;
}
