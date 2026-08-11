import { dependenceKind } from "../labels.js";

/**
 * The sub-questions a decomposition says do not move independently.
 *
 * Read-only, and shared by the run tree and the synthesis view so the two cannot describe
 * the same grouping differently.
 *
 * A group carries a dependence parameter that slides its combined rate away from the plain
 * product, so a reader who multiplies the rates on screen will not reproduce the anchor.
 * This block is what explains the gap. It renders nothing when nothing is grouped, which
 * is the common case and correctly reads as "everything is independent".
 */
export default function DependentGroups({ decomposition }) {
  const groups = decomposition?.dependent_groups || [];
  if (!groups.length) return null;

  return (
    <div className="card-sub" style={{ marginTop: 8 }}>
      <span className="micro">Sub-questions that move together</span>
      {groups.map((g, i) => {
        const kind = dependenceKind(g.kind);
        return (
          <div key={i} style={{ marginTop: 6 }}>
            <div className="src-chips" style={{ marginTop: 0 }}>
              {(g.members || []).map((m) => (
                <span key={m} className="src-chip">
                  sq{m}
                </span>
              ))}
              <span className="chip" title={`dependence ${kind.w.toFixed(2)}`}>
                {kind.label}
              </span>
            </div>
            <div style={{ marginTop: 4 }}>
              {g.name}
              {g.name ? " — " : ""}
              {kind.description}
            </div>
          </div>
        );
      })}
    </div>
  );
}
