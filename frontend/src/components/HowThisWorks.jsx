import Accordion from "./Accordion.jsx";

/**
 * What one stage does, and how the run executes it.
 *
 * The tree shows what each stage produced but never says what the stage was for, so a
 * reader meeting the pipeline for the first time has to infer the method from its output.
 * Static copy, collapsed by default — a reader who already knows the pipeline should not
 * have to scroll past it.
 *
 * Keep these in step with `superforecaster/agents/` and `superforecaster/stages.py`. A
 * panel that describes tools an agent no longer has is worse than no panel.
 */
const STAGE_HELP = {
  decompose: {
    does: "Splits the question into 3–5 sub-questions, labels each one researchable or judgment, and names the rule that combines them back into the whole question. Sub-questions that move together are grouped, so the chain does not treat them as independent.",
    how: "One agent call, no search tools. Edit the result while everything derived from it is still pending.",
  },
  lenses: {
    does: "Names 1–3 reference populations for one sub-question and weighs each by how well it fits the case. A population has to be defined precisely enough that someone else could count the same cases.",
    how: "One agent call per researchable sub-question, no search tools. The agent never sees a rate — naming the populations before measuring them is what stops the run settling on whichever population gave the answer it liked.",
  },
  base_rates: {
    does: "Counts how often the population the lens named resolved yes, and reports it as hits over n. The rate is arithmetic over the evidence blocks, never a number the agent asserts.",
    how: "One agent call per (sub-question, lens) cell, with web and Wikipedia search. A counted block has to list its cases; a published statistic has to name its source.",
  },
  inside_view: {
    does: "Lists what makes this case differ from the population, and how far each difference moves the lens's measured rate. Every modifier states what the opposite evidence would have done — evidence that fails its own flip test moves the number by zero.",
    how: "One agent call per cell, with web and Wikipedia search plus a tool that looks for evidence against the emerging answer. Only the modifiers naming a lens move that lens.",
  },
  synthesis: {
    does: "Combines every adjusted lens into one probability. The anchor and the probability its own parts imply are computed first, then the agent commits to a number within the configured slack and writes the rationale.",
    how: "The arithmetic runs in `checks.py`, not in the model. The same checks then critique the result and send one retry back if a blocking principle failed. A clean pass saves the forecast.",
  },
};

/** Every stage runs the same way, so this is said once rather than five times. */
const SHARED =
  "Each cell is one request you start, and it streams while it runs. A stage unlocks " +
  "only once every earlier stage is complete.";

export default function HowThisWorks({ stage }) {
  const help = STAGE_HELP[stage];
  if (!help) return null;

  return (
    <Accordion
      className="how"
      summary={<span className="grow">How this works</span>}
    >
      <div className="how-block">
        <div className="micro">What it does</div>
        <div className="card-sub">{help.does}</div>
      </div>
      <div className="how-block">
        <div className="micro">How it is run</div>
        <div className="card-sub">
          {help.how} {SHARED}
        </div>
      </div>
    </Accordion>
  );
}
