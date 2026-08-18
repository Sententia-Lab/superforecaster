import Accordion from "./Accordion.jsx";
import { domainOf } from "../derive.js";

/**
 * Every source one cell retrieved, read back from its persisted payload.
 *
 * The stream pushes a `source` chip as each tool result lands and then loses it — the
 * chips live in `useStepStream` state, which the request ends. `BaseRateStepPayload` and
 * `InsideStepPayload` both store the same URLs, so a finished run can still answer "what
 * did this cell read" after a reload.
 *
 * Collapsed by default. A cell can retrieve twenty pages and cite three of them; the
 * three that a claim rests on already appear next to that claim.
 */
export default function SourceList({ sources }) {
  const rows = byUrl(sources);
  if (!rows.length) return null;

  return (
    <Accordion
      summary={
        <>
          <span className="chip">{rows.length}</span>
          <span className="grow">Sources retrieved</span>
        </>
      }
    >
      {rows.map((s) => (
        <div key={s.url} className="source-row">
          <a href={s.url} target="_blank" rel="noreferrer" title={s.url}>
            {s.title || domainOf(s.url)}
          </a>
          <div className="micro">
            {[domainOf(s.url), s.query && `"${s.query}"`, s.tool]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
      ))}
    </Accordion>
  );
}

/** One row per URL. A second search returning the same page is not a second source. */
function byUrl(sources) {
  const seen = new Map();
  for (const s of sources || []) {
    if (s?.url && !seen.has(s.url)) seen.set(s.url, s);
  }
  return [...seen.values()];
}
