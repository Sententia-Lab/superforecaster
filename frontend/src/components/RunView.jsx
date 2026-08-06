import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useStepStream } from "../hooks/useStepStream.js";
import CellActivity from "./CellActivity.jsx";
import LensCard from "./LensCard.jsx";
import LiveTail from "./LiveTail.jsx";
import StepControls from "./StepControls.jsx";
import SynthesisSection from "./SynthesisSection.jsx";

const STAGE_TITLES = {
  decompose: "Decompose",
  lenses: "Find lenses",
  base_rates: "Base rates",
  inside_view: "Inside view",
  synthesis: "Synthesis",
};

function StageSection({ n, title, children, note }) {
  return (
    <section className="stage">
      <div className="stage-head">
        <span className="n">{n}</span>
        <span className="title">{title}</span>
        {note ? <span className="hint">{note}</span> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * The gated pipeline (and, once complete, the saved forecast — same tree, no
 * buttons left to press). Stages stack vertically; section 1 is the only raw live
 * tail; sections 2–4 render cards up front with processing inside the active card;
 * section 5 is arithmetic + probability + rationale + violations.
 */
export default function RunView({ runId, onChanged }) {
  const [run, setRun] = useState(null);
  const [loadError, setLoadError] = useState("");

  const refreshDetail = useCallback(() => {
    api
      .getRun(runId)
      .then(setRun)
      .catch((e) => setLoadError(e.message));
  }, [runId]);

  useEffect(refreshDetail, [refreshDetail]);

  const stream = useStepStream({
    onRun: (payload) => {
      setRun(payload);
      onChanged();
    },
    onDone: () => {
      refreshDetail();
      onChanged();
    },
  });

  if (!run) {
    return loadError ? <div className="error-banner">{loadError}</div> : null;
  }

  const steps = run.steps || [];
  const byStage = (stage) => steps.filter((s) => s.stage === stage);
  const stepFor = (stage, subClaimId = "", lensName = "") =>
    steps.find(
      (s) =>
        s.stage === stage &&
        s.sub_claim_id === subClaimId &&
        s.lens_name === lensName,
    );

  const decomposeStep = stepFor("decompose");
  const decomposition = decomposeStep?.payload || null;
  const researchable = (decomposition?.sub_claims || []).filter(
    (s) => s.knowability === "researchable",
  );
  const synthesisStep = stepFor("synthesis");

  const busy = stream.active !== null;
  const activeFor = (step) =>
    stream.active?.stepId === step.id ? stream.active : null;
  const start = (step) =>
    ({ deeper } = {}) =>
      stream.start(run.id, step.id, {
        maxIterations: deeper ? run.max_iterations * 2 : undefined,
      });

  const subClaimById = Object.fromEntries(
    (decomposition?.sub_claims || []).map((s) => [s.id, s]),
  );

  const cellSection = (n, stage, title, note) => {
    const cells = byStage(stage);
    if (!cells.length) return null;
    const bySubClaim = {};
    for (const c of cells) (bySubClaim[c.sub_claim_id] ??= []).push(c);

    return (
      <StageSection n={n} title={title} note={note}>
        {Object.entries(bySubClaim).map(([scId, scCells]) => (
          <div key={scId} style={{ marginBottom: 12 }}>
            <div className="card-sub" style={{ margin: "0 2px 6px" }}>
              <b>{scId}</b> — {subClaimById[scId]?.question}
            </div>
            {scCells.map((cell) => {
              const baseStep =
                stage === "inside_view"
                  ? stepFor("base_rates", cell.sub_claim_id, cell.lens_name)
                  : cell;
              const chosen = chosenLens(
                steps,
                cell.sub_claim_id,
                cell.lens_name,
              );
              return (
                <LensCard
                  key={cell.id}
                  step={cell}
                  lens={baseStep?.payload?.lens || chosen}
                  researched={baseStep?.payload}
                  insidePayload={stage === "inside_view" ? cell.payload : null}
                  active={activeFor(cell)}
                  busy={busy}
                  onStart={start(cell)}
                />
              );
            })}
          </div>
        ))}
      </StageSection>
    );
  };

  return (
    <div>
      <h1 className="qtitle">{run.question}</h1>
      <div className="qmeta">
        Resolves <b>{(run.resolution_date || "").slice(0, 10)}</b> via{" "}
        <b>{run.resolution_source}</b> — {run.resolution_criteria}
        {run.status === "complete" && (
          <>
            {" "}
            <span className="chip green">complete</span>
          </>
        )}
      </div>
      {run.error && !busy && (
        <div className="error-banner">
          This run hit an error: {run.error}. Retry the failed step below.
        </div>
      )}

      <StageSection n={1} title={STAGE_TITLES.decompose}>
        {decomposeStep &&
          (activeFor(decomposeStep) ? (
            <div className="card">
              <div className="card-head">
                <span className="headline">
                  <span className="spinner" /> Decomposing the question…
                </span>
              </div>
              <LiveTail text={activeFor(decomposeStep).thoughts} />
            </div>
          ) : decomposition ? (
            <div className="card">
              {decomposition.sub_claims.map((s) => (
                <div key={s.id} className="evidence-row">
                  <b style={{ flex: "none" }}>{s.id}</b>
                  <span className="grow">{s.question}</span>
                  <span className="chip">
                    {s.knowability === "researchable" ? "researchable" : "judgment"}
                  </span>
                </div>
              ))}
              <div className="card-sub" style={{ marginTop: 8 }}>
                Chain rule: <b>{decomposition.chain_rule}</b> —{" "}
                {decomposition.chain_note}
              </div>
            </div>
          ) : (
            <div className="card">
              <div className="card-head">
                <span className="headline">
                  Break the question into 3–5 sub-questions
                </span>
              </div>
              <StepControls
                step={decomposeStep}
                label="Run decomposition"
                busy={busy}
                onStart={start(decomposeStep)}
              />
            </div>
          ))}
      </StageSection>

      {byStage("lenses").length > 0 && (
        <StageSection
          n={2}
          title={STAGE_TITLES.lenses}
          note="All sub-questions need lenses before base rates unlock."
        >
          {byStage("lenses").map((step) => {
            const sc = subClaimById[step.sub_claim_id];
            const active = activeFor(step);
            return (
              <div key={step.id} className="card">
                <div className="card-head">
                  <span className="headline">
                    {step.sub_claim_id} — {sc?.question}
                  </span>
                </div>
                {sc?.rationale && <div className="card-sub">{sc.rationale}</div>}
                {active ? (
                  <CellActivity active={active} />
                ) : step.status === "complete" ? (
                  <div style={{ marginTop: 8 }}>
                    {(step.payload?.lenses || []).map((l) => (
                      <div key={l.name} className="evidence-row">
                        <b style={{ flex: "none" }}>{l.name}</b>
                        <span className="grow">{l.population}</span>
                        <span className="chip">w {l.weight}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <StepControls
                    step={step}
                    label="Find lenses"
                    busy={busy}
                    onStart={start(step)}
                  />
                )}
              </div>
            );
          })}
        </StageSection>
      )}

      {cellSection(
        3,
        "base_rates",
        STAGE_TITLES.base_rates,
        "Each lens gets its own counted rate.",
      )}
      {cellSection(
        4,
        "inside_view",
        STAGE_TITLES.inside_view,
        "Modifiers move each lens's measured rate.",
      )}

      {synthesisStep && (
        <StageSection n={5} title={STAGE_TITLES.synthesis}>
          {activeFor(synthesisStep) ? (
            <div className="card">
              <div className="card-head">
                <span className="headline">
                  <span className="spinner" /> Reflecting, synthesizing,
                  critiquing…
                </span>
              </div>
              <CellActivity active={activeFor(synthesisStep)} />
            </div>
          ) : synthesisStep.status === "complete" && synthesisStep.payload ? (
            <SynthesisSection
              payload={synthesisStep.payload}
              decomposition={decomposition}
            />
          ) : (
            <div className="card">
              <div className="card-head">
                <span className="headline">
                  Compute the anchor, then commit to a number
                </span>
              </div>
              <div className="card-sub">
                Arithmetic first (not agentic), then the synthesis agent adjusts
                within the configured slack and the checks critique it.
              </div>
              <StepControls
                step={synthesisStep}
                label="Run synthesis"
                busy={busy}
                onStart={start(synthesisStep)}
              />
            </div>
          )}
        </StageSection>
      )}
    </div>
  );
}

function chosenLens(steps, subClaimId, lensName) {
  const lensesStep = steps.find(
    (s) => s.stage === "lenses" && s.sub_claim_id === subClaimId,
  );
  return (lensesStep?.payload?.lenses || []).find((l) => l.name === lensName);
}
