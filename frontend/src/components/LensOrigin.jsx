import Prose from "./Prose.jsx";

/**
 * The lens, restated inside a cell that was measured through it.
 *
 * Base rates and modifiers are net-new analysis; the name and the population are not —
 * they were chosen at the lenses stage, before anything was measured. Without a visible
 * boundary the two read as one block of text, and a reader cannot tell which claims the
 * cell is responsible for.
 */
export default function LensOrigin({ lens }) {
  if (!lens) return null;
  return (
    <div className="lens-origin">
      <div className="micro">From the lens</div>
      <div className="lens-origin-name">
        {lens.name}
        {typeof lens.weight === "number" && (
          <span className="chip">w {lens.weight}</span>
        )}
      </div>
      {lens.population && <Prose className="prose tight">{lens.population}</Prose>}
    </div>
  );
}
