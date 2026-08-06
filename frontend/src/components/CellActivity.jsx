import { domainOf } from "../derive.js";

/**
 * Compact in-card processing view: the current query line, source chips as they
 * arrive, and a one-line thought pulse. Deliberately not a raw tail — the card's
 * headline stays the star; this is what "processing inside the card" looks like.
 */
export default function CellActivity({ active }) {
  const lastThought = (active.thoughts || "").split("\n").filter(Boolean).pop() || "";
  return (
    <div className="activity">
      <div>
        <span className="spinner" />{" "}
        <span className="thought">
          {active.query || lastThought.slice(-140) || "working…"}
        </span>
      </div>
      {active.query && lastThought ? (
        <div className="qline">{lastThought.slice(-160)}</div>
      ) : null}
      {active.sources.length > 0 && (
        <div className="src-chips">
          {active.sources.slice(-8).map((s, i) => (
            <span key={i} className="src-chip" title={s.title || s.url}>
              {s.title || domainOf(s.url)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
