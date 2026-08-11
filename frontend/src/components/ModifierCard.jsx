import Accordion from "./Accordion.jsx";
import CellActivity from "./CellActivity.jsx";
import LensOrigin from "./LensOrigin.jsx";
import Prose from "./Prose.jsx";
import StepControls from "./StepControls.jsx";
import { domainOf, lensRate, pct, signedAdjustment } from "../derive.js";
import { firstSentence, ordinal } from "../labels.js";

/** `+0.10`, `−0.04`, `±0.00`. The minus is U+2212, matching the arithmetic line. */
function magnitude(a) {
  if (a.is_noise) return "±0.00";
  return `${a.direction === "down" ? "−" : "+"}${a.magnitude.toFixed(2)}`;
}

function Modifier({ adjustment: a, index }) {
  return (
    <div className="mod">
      <div className="mod-title">
        <span className={`chip adj-mag ${a.is_noise ? "noise" : a.direction}`}>
          {magnitude(a)}
        </span>
        <span className="name">
          {ordinal("Modifier", index)} — {a.title || firstSentence(a.evidence)}
        </span>
        {a.is_noise && <span className="chip">noise — flip test failed</span>}
      </div>
      <div className="mod-body">
        <Prose>{a.evidence}</Prose>
        {a.sources?.length ? (
          <div className="src-chips">
            {a.sources
              .filter((s) => s.url)
              .map((s, i) => (
                <a key={i} className="src-chip" href={s.url} target="_blank" rel="noreferrer">
                  {s.source || domainOf(s.url)}
                </a>
              ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/**
 * One (sub-question, lens) cell at the inside-view stage.
 *
 * Each modifier leads with its signed move and its title, so the list can be scanned
 * without reading three paragraphs. The arithmetic line underneath is the same one the
 * synthesis table recomputes.
 */
export default function ModifierCard({
  step,
  lens,
  researched,
  insidePayload,
  active,
  error,
  busy,
  onStart,
}) {
  const measured = researched?.lens;
  const complete = step.status === "complete" && insidePayload;

  const summary = (
    <>
      <b style={{ flex: "none" }}>Modifiers</b>
      <span className="grow">{lens?.name || step.lens_name}</span>
      {complete && measured && (
        <span className="rate">
          {pct(lensRate(measured))} → {pct(adjustedRate(measured, insidePayload))}
        </span>
      )}
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
            {insidePayload.adjustments.map((a, i) => (
              <Modifier key={i} adjustment={a} index={i} />
            ))}

            {measured && (
              <div className="card-sub" style={{ marginTop: 6 }}>
                {pct(lensRate(measured))}{" "}
                {insidePayload.adjustments
                  .filter((a) => !a.is_noise && a.direction !== "neutral")
                  .map((a) => `${a.direction === "down" ? "−" : "+"} ${a.magnitude.toFixed(2)}`)
                  .join(" ")}{" "}
                → <b className="rate">{pct(adjustedRate(measured, insidePayload))}</b>
              </div>
            )}

            {insidePayload.steel_man ? (
              <Accordion summary={<span className="grow">Steel man</span>}>
                <Prose>{insidePayload.steel_man}</Prose>
              </Accordion>
            ) : null}
          </>
        ) : (
          <StepControls
            step={step}
            label="Find modifiers"
            busy={busy}
            onStart={onStart}
            error={error}
          />
        )}
      </Accordion>
    </div>
  );
}

function adjustedRate(measured, insidePayload) {
  const moved = insidePayload.adjustments.reduce((n, a) => n + signedAdjustment(a), 0);
  return Math.min(1, Math.max(0, lensRate(measured) + moved));
}
