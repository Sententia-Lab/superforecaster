// Pure derivations mirroring `checks.py`. The check and the picture have to agree
// about what the evidence implies, so they compute it the same way.
//
// `editBlocker` and `normalizeWeights` mirror `machine.edit_blocker` and
// `stages.normalize_weights` for the same reason: the lock the screen draws and the lock
// the server enforces must be the same lock, or an Edit pencil appears on a payload the
// API will refuse.

/** Which rows exist only because of this stage's payload. Mirrors `machine.DERIVED`. */
const DERIVED = {
  // A decomposition with no researchable sub-claims fans out straight to synthesis.
  decompose: (steps) =>
    steps.filter((s) => s.stage === "lenses" || s.stage === "synthesis"),
  lenses: (steps, step) =>
    steps.filter(
      (s) => s.stage === "base_rates" && s.sub_claim_id === step.sub_claim_id,
    ),
};

/**
 * Null while the payload may still be edited; otherwise what already ran.
 *
 * A payload is editable exactly while everything derived from it is untouched, so an
 * edit can only ever strand empty pending rows.
 */
export function editBlocker(step, steps) {
  if (!step || !DERIVED[step.stage]) return "not editable";
  if (step.status !== "complete") return `step is ${step.status}`;
  const ran = DERIVED[step.stage](steps, step).find(
    (s) => s.status !== "pending",
  );
  return ran ? `${ran.stage} is ${ran.status}` : null;
}

/**
 * Rescale a lens set to sum to 1.00 at two decimals, by largest remainder.
 *
 * Mirrors `stages.normalize_weights`, and drives the Normalize button. Floors each share
 * at 0.01 because a weight of exactly zero is not a legal lens.
 */
export function normalizeWeights(lenses) {
  const total = lenses.reduce((t, l) => t + (Number(l.weight) || 0), 0);
  if (total <= 0) return lenses;

  const budget = 100 - lenses.length;
  const exact = lenses.map((l) => ((Number(l.weight) || 0) / total) * budget);
  const shares = exact.map((x) => 1 + Math.floor(x));

  const order = exact
    .map((x, i) => [x - Math.floor(x), i])
    .sort((a, b) => b[0] - a[0]);
  for (let i = 0; i < 100 - shares.reduce((t, s) => t + s, 0); i++) {
    shares[order[i][1]] += 1;
  }

  return lenses.map((l, i) => ({ ...l, weight: shares[i] / 100 }));
}

/** Σ of a lens set's weights, rounded the way the Σ chip displays it. */
export function weightSum(lenses) {
  return Math.round(lenses.reduce((t, l) => t + (Number(l.weight) || 0), 0) * 100) / 100;
}

/** The populations a sub-question was viewed through. */
export function lensesFor(id, outside) {
  if (!outside || !id) return [];
  return outside.lenses.filter((l) => (l.sub_claim_ids || []).includes(id));
}

/** A lens's base rate: pooled hits over pooled n. Derived, never asserted. */
export function lensRate(lens) {
  const n = (lens.evidence || []).reduce((t, e) => t + e.n, 0);
  if (!n) return 0;
  return (lens.evidence || []).reduce((t, e) => t + e.hits, 0) / n;
}

/** How many cases a lens rests on, and how they were gathered. */
export function lensEvidenceSummary(lens) {
  return (lens.evidence || []).map((e) => `${e.hits} of ${e.n} ${e.kind}`).join(" · ");
}

/** The modifiers that move one lens. */
export function adjustmentsForLens(lens, inside) {
  if (!inside) return [];
  return inside.adjustments.filter((a) => a.lens_name === lens.name);
}

/** An adjustment's signed contribution. Noise moves the number by zero, by definition. */
export function signedAdjustment(a) {
  if (a.is_noise) return 0;
  if (a.direction === "up") return a.magnitude;
  if (a.direction === "down") return -a.magnitude;
  return 0;
}

/** A lens's rate after its own modifiers. Only adjustments naming it apply. */
export function adjustedLensRate(lens, inside) {
  const moved = adjustmentsForLens(lens, inside).reduce(
    (n, a) => n + signedAdjustment(a),
    0,
  );
  return Math.min(1, Math.max(0, lensRate(lens) + moved));
}

/**
 * A sub-question's rate: its adjusted lenses blended by relevance.
 *
 * `n` is deliberately absent — sample size says how well a population was measured,
 * not how much it resembles this case. Mirrors `checks.sub_claim_rate`.
 */
export function subClaimRate(id, outside, inside) {
  const lenses = lensesFor(id, outside);
  const total = lenses.reduce((n, l) => n + l.weight, 0);
  if (!lenses.length || total <= 1e-9) return null;
  return (
    lenses.reduce((n, l) => n + l.weight * adjustedLensRate(l, inside), 0) / total
  );
}

const CONFIDENCE_RANK = { low: 1, medium: 2, high: 3 };

/** A claim is graded by its strongest source, so an extra thin one changes nothing. */
export function claimSupport(sources) {
  if (!sources || !sources.length) return "";
  return sources.reduce(
    (best, s) =>
      (CONFIDENCE_RANK[s.confidence] || 0) > (CONFIDENCE_RANK[best] || 0)
        ? s.confidence
        : best,
    "",
  );
}

/** The graded sources behind a lens, which live on its evidence blocks. */
export function lensSources(lens) {
  return (lens.evidence || []).map((e) => e.source).filter(Boolean);
}

export function pct(x, digits = 1) {
  if (x === null || x === undefined) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}

export function domainOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
