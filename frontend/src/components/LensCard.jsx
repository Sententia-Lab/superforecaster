import CellActivity from "./CellActivity.jsx";
import StepControls from "./StepControls.jsx";
import {
  claimSupport,
  lensEvidenceSummary,
  lensRate,
  lensSources,
  pct,
  signedAdjustment,
} from "../derive.js";

function SupportChip({ sources }) {
  const support = claimSupport(sources);
  if (!support) return null;
  const tone = { high: "green", medium: "yellow", low: "red" }[support] || "";
  return <span className={`chip ${tone}`}>{support} support</span>;
}

function EvidenceList({ lens }) {
  return (
    <div style={{ marginTop: 8 }}>
      {(lens.evidence || []).map((e, i) => (
        <div key={i} className="evidence-row">
          <span className="rate">
            {e.hits}/{e.n}
          </span>
          <span className="chip">{e.kind}</span>
          <span className="grow">{e.note}</span>
          {e.source?.url ? (
            <a href={e.source.url} target="_blank" rel="noreferrer">
              {e.source.source}
            </a>
          ) : (
            <span>{e.source?.source || ""}</span>
          )}
        </div>
      ))}
    </div>
  );
}

function AdjustmentRows({ lens, adjustments }) {
  return (
    <div style={{ marginTop: 8 }}>
      {adjustments.map((a, i) => (
        <div key={i} className="adj-row">
          <span
            className={`adj-mag ${a.is_noise ? "noise" : a.direction}`}
          >
            {a.is_noise
              ? "±0.00"
              : `${a.direction === "down" ? "−" : "+"}${a.magnitude.toFixed(2)}`}
          </span>
          <span className="grow">
            {a.evidence}
            {a.is_noise ? " (noise — flip test failed)" : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * One (sub-question, lens) cell, in either the base-rates or inside-view section.
 * The headline (population) renders up front; processing lives inside the card;
 * the result — a counted rate, or the modifiers that move it — appends at the
 * bottom. Non-negotiable layout, restated as a component.
 */
export default function LensCard({
  step,
  lens, // chosen Lens (base_rates) or researched lens payload (inside_view)
  researched, // BaseRateStepPayload once measured
  insidePayload, // InsideStepPayload once adjusted
  active, // stream state when this card's step is running
  busy,
  onStart,
}) {
  const measured = researched?.lens;
  return (
    <div className="card nested">
      <div className="card-head">
        <span className="headline">{lens?.name || step.lens_name}</span>
        {measured && step.stage === "base_rates" && step.status === "complete" && (
          <span className="rate">{pct(lensRate(measured))}</span>
        )}
        {measured && <SupportChip sources={lensSources(measured)} />}
      </div>
      {lens?.population && <div className="card-sub">{lens.population}</div>}
      {lens?.why_it_fits && (
        <div className="card-sub" style={{ fontStyle: "italic" }}>
          {lens.why_it_fits} (weight {lens.weight})
        </div>
      )}

      {active ? (
        <CellActivity active={active} />
      ) : step.status === "complete" ? (
        step.stage === "base_rates" && measured ? (
          <>
            <div className="card-sub" style={{ marginTop: 6 }}>
              Counted rate: <b className="rate">{pct(lensRate(measured))}</b>{" "}
              from {lensEvidenceSummary(measured)}
            </div>
            <EvidenceList lens={measured} />
            {researched.disagreement ? (
              <div className="card-sub" style={{ marginTop: 6 }}>
                Disagreement: {researched.disagreement}
              </div>
            ) : null}
          </>
        ) : step.stage === "inside_view" && insidePayload ? (
          <>
            <AdjustmentRows
              lens={measured}
              adjustments={insidePayload.adjustments}
            />
            {measured && (
              <div className="card-sub" style={{ marginTop: 6 }}>
                {pct(lensRate(measured))}{" "}
                {insidePayload.adjustments
                  .filter((a) => !a.is_noise && a.direction !== "neutral")
                  .map(
                    (a) =>
                      `${a.direction === "down" ? "−" : "+"} ${a.magnitude.toFixed(2)}`,
                  )
                  .join(" ")}{" "}
                → <b className="rate">{pct(adjustedRate(measured, insidePayload))}</b>
              </div>
            )}
            {insidePayload.steel_man ? (
              <div className="card-sub" style={{ marginTop: 6 }}>
                Steel man: {insidePayload.steel_man}
              </div>
            ) : null}
          </>
        ) : null
      ) : (
        <StepControls
          step={step}
          label={
            step.stage === "base_rates" ? "Find base rate" : "Find modifiers"
          }
          busy={busy}
          onStart={onStart}
        />
      )}
    </div>
  );
}

function adjustedRate(measured, insidePayload) {
  const moved = insidePayload.adjustments.reduce(
    (n, a) => n + signedAdjustment(a),
    0,
  );
  return Math.min(1, Math.max(0, lensRate(measured) + moved));
}
