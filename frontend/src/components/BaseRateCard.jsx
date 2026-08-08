import Accordion from "./Accordion.jsx";
import CellActivity from "./CellActivity.jsx";
import LensOrigin from "./LensOrigin.jsx";
import Prose from "./Prose.jsx";
import StepControls from "./StepControls.jsx";
import { claimSupport, domainOf, lensEvidenceSummary, lensRate, lensSources, pct } from "../derive.js";
import { ordinal } from "../labels.js";

function SupportChip({ sources }) {
  const support = claimSupport(sources);
  if (!support) return null;
  const tone = { high: "green", medium: "yellow", low: "red" }[support] || "";
  return <span className={`chip ${tone}`}>{support} support</span>;
}

function SourceLinks({ evidence, cellSources }) {
  if (evidence.source?.url) {
    return (
      <div className="src-chips">
        <a className="src-chip" href={evidence.source.url} target="_blank" rel="noreferrer">
          {evidence.source.source || domainOf(evidence.source.url)}
        </a>
      </div>
    );
  }
  if (evidence.source?.source) {
    return <div className="card-sub">{evidence.source.source}</div>;
  }
  // A counted block usually cites nothing of its own, so fall back to what the cell
  // actually fetched. Saying "no source retrieved" out loud beats an empty space that
  // could equally mean "sourced, not shown".
  if (cellSources?.length) {
    return (
      <div className="src-chips">
        {cellSources.map((s, i) => (
          <a
            key={i}
            className="src-chip"
            href={s.url}
            target="_blank"
            rel="noreferrer"
            title={s.title || s.url}
          >
            {s.title || domainOf(s.url)}
          </a>
        ))}
      </div>
    );
  }
  return <div className="card-sub">No source retrieved.</div>;
}

/**
 * One (sub-question, lens) cell at the base-rates stage.
 *
 * The lens is restated in its own panel so a reader can see which text was chosen before
 * anything was measured. Everything below that panel is what this cell found. The
 * enumeration behind each fraction is long enough to bury the card, so it collapses.
 */
export default function BaseRateCard({ step, index, lens, researched, active, busy, onStart }) {
  const measured = researched?.lens;
  const complete = step.status === "complete" && measured;

  const summary = (
    <>
      <b style={{ flex: "none" }}>{ordinal("Base rate", index)}</b>
      <span className="grow">{lens?.name || step.lens_name}</span>
      {typeof lens?.weight === "number" && <span className="chip">w {lens.weight}</span>}
      {complete && <span className="rate">{pct(lensRate(measured))}</span>}
      {measured && <SupportChip sources={lensSources(measured)} />}
    </>
  );

  return (
    <div className="card nested">
      <Accordion defaultOpen summary={summary}>
        <LensOrigin lens={lens} />

        {active ? (
          <CellActivity active={active} />
        ) : complete ? (
          <>
            <div className="card-sub">
              Counted rate: <b className="rate">{pct(lensRate(measured))}</b> from{" "}
              {lensEvidenceSummary(measured)}
            </div>

            {(measured.evidence || []).map((e, i) => (
              <Accordion
                key={i}
                className="counted"
                summary={
                  <>
                    <span className="rate" style={{ flex: "none" }}>
                      {e.hits}/{e.n}
                    </span>
                    <span className="chip">{e.kind}</span>
                    <span className="grow">How this was counted</span>
                  </>
                }
              >
                <Prose>{e.note}</Prose>
                <SourceLinks evidence={e} cellSources={researched.sources} />
              </Accordion>
            ))}

            {researched.disagreement ? (
              <Accordion summary={<span className="grow">Disagreement</span>}>
                <Prose>{researched.disagreement}</Prose>
              </Accordion>
            ) : null}
          </>
        ) : (
          <StepControls
            step={step}
            label="Find base rate"
            busy={busy}
            onStart={onStart}
          />
        )}
      </Accordion>
    </div>
  );
}
