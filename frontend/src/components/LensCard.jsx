import Accordion from "./Accordion.jsx";
import Prose from "./Prose.jsx";
import { ordinal } from "../labels.js";

/**
 * One lens at the lenses stage.
 *
 * The same card a measured cell gets: a nested card holding one accordion, so a lens set
 * reads as a list of cells rather than as one long card. `population`, `why_it_fits` and
 * `weight_rationale` answer three different questions, so each gets a labelled block —
 * without the labels the three run together as one paragraph and a reader cannot tell
 * the boundary of the population from the argument for its weight.
 */
export default function LensCard({ lens, index }) {
  const summary = (
    <>
      <b style={{ flex: "none" }}>{ordinal("Lens", index)}</b>
      <span className="grow">{lens.name}</span>
      <span className="chip">w {lens.weight}</span>
    </>
  );

  return (
    <div className="card nested">
      <Accordion defaultOpen summary={summary}>
        <div className="lens-block">
          <div className="micro">Population</div>
          <Prose className="prose tight">{lens.population}</Prose>
        </div>

        {lens.why_it_fits && (
          <div className="lens-block">
            <div className="micro">Why it fits</div>
            <Prose className="prose tight">{lens.why_it_fits}</Prose>
          </div>
        )}

        {lens.weight_rationale && (
          <div className="lens-block">
            <div className="micro">Weight {lens.weight}</div>
            <Prose className="prose tight">{lens.weight_rationale}</Prose>
          </div>
        )}
      </Accordion>
    </div>
  );
}
