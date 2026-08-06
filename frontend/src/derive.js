// Pure derivations mirroring `checks.py`. The check and the picture have to agree
// about what the evidence implies, so they compute it the same way.

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
