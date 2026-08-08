import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { editBlocker } from "../derive.js";
import { useRunQueue } from "../hooks/useRunQueue.js";
import { useStepStream } from "../hooks/useStepStream.js";
import { sectionRunnable } from "../runQueue.js";
import Accordion from "./Accordion.jsx";
import BaseRateCard from "./BaseRateCard.jsx";
import CellActivity from "./CellActivity.jsx";
import ConfirmDialog from "./ConfirmDialog.jsx";
import DecomposeEditor from "./DecomposeEditor.jsx";
import LensSetEditor from "./LensSetEditor.jsx";
import LiveTail from "./LiveTail.jsx";
import ModifierCard from "./ModifierCard.jsx";
import Prose from "./Prose.jsx";
import RunHeader from "./RunHeader.jsx";
import StepControls from "./StepControls.jsx";
import SynthesisSection from "./SynthesisSection.jsx";
import { ordinal, subQuestionLabel } from "../labels.js";

const STAGE_TITLES = {
  decompose: "Decompose",
  lenses: "Find lenses",
  base_rates: "Base rates",
  inside_view: "Inside view",
  synthesis: "Synthesis",
};

/**
 * One stage, collapsible.
 *
 * `open` is passed rather than held: sections stay open while a run is in flight and
 * collapse once it is finished, so a completed run opens on its answer instead of on the
 * top of a very long scroll.
 */
function StageSection({ n, title, children, note, action, open }) {
  return (
    <details className="stage acc" open={open}>
      <summary className="stage-head">
        <span className="n">{n}</span>
        <span className="title">{title}</span>
        {note ? <span className="hint">{note}</span> : null}
        {/* A click on the button must not also toggle the section it lives in. */}
        {action ? <span onClick={(e) => e.stopPropagation()}>{action}</span> : null}
      </summary>
      <div className="acc-body">{children}</div>
    </details>
  );
}

/** The lock chip, or the Edit pencil, for one editable payload. */
function EditGate({ step, steps, busy, onEdit }) {
  const blocker = editBlocker(step, steps);
  if (blocker) {
    return (
      <span className="chip lock" title={`Locked — ${blocker}`}>
        locked
      </span>
    );
  }
  return (
    <button className="btn tiny ghost" disabled={busy} onClick={onEdit}>
      Edit
    </button>
  );
}

/**
 * The gated pipeline (and, once complete, the saved forecast — same tree, no
 * buttons left to press). Stages stack vertically; section 1 is the only raw live
 * tail; sections 2–4 render cards up front with processing inside the active card;
 * section 5 is arithmetic + probability + rationale + violations.
 */
export default function RunView({ runId, onChanged, onDeleted }) {
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
  const queue = useRunQueue({ stream });

  const [editing, setEditing] = useState(null); // step id being edited
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  const savePayload = useCallback(
    async (stepId, payload) => {
      setSaving(true);
      setSaveError("");
      try {
        // The response is the whole run, so the tree redraws without a follow-up GET.
        setRun(await api.editStepPayload(runId, stepId, payload));
        setEditing(null);
        onChanged();
      } catch (e) {
        setSaveError(e.message);
      } finally {
        setSaving(false);
      }
    },
    [runId, onChanged],
  );

  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  const deleteRun = useCallback(async () => {
    setDeleting(true);
    setDeleteError("");
    try {
      await api.deleteRun(runId);
      onDeleted();
    } catch (e) {
      setDeleteError(e.message);
      setDeleting(false);
    }
  }, [runId, onDeleted]);

  const runAll = useCallback(async () => {
    let tree = run;
    if (tree.status === "backlog") {
      tree = await api.startRun(tree.id);
      setRun(tree);
      onChanged();
    }
    await queue.drain("all", tree);
  }, [run, queue, onChanged]);

  if (!run) {
    return loadError ? <div className="error-banner">{loadError}</div> : null;
  }

  const steps = run.steps || [];
  const byStage = (stage) => steps.filter((s) => s.stage === stage);
  const stepFor = (stage, subQuestionId = "", lensName = "") =>
    steps.find(
      (s) =>
        s.stage === stage &&
        s.sub_question_id === subQuestionId &&
        s.lens_name === lensName,
    );

  const decomposeStep = stepFor("decompose");
  const decomposition = decomposeStep?.payload || null;
  const researchable = (decomposition?.sub_questions || []).filter(
    (s) => s.knowability === "researchable",
  );
  const synthesisStep = stepFor("synthesis");
  // A finished run leads with its answer, and the stages that produced it start folded.
  const done = synthesisStep?.status === "complete";

  // "A request is in flight", not "there is an error on screen". Deriving this from
  // `stream.active` left every button disabled after a failure, because the error card
  // deliberately outlives the request that produced it.
  const busy = stream.streaming;
  const activeFor = (step) =>
    stream.active?.stepId === step.id ? stream.active : null;
  const start = (step) =>
    ({ deeper } = {}) =>
      stream.start(run.id, step.id, {
        maxIterations: deeper ? run.max_iterations * 2 : undefined,
      });

  const subQuestionById = Object.fromEntries(
    (decomposition?.sub_questions || []).map((s) => [s.id, s]),
  );

  /** Run one stage and stop, so you review between stages instead of at every cell. */
  const sectionAction = (stage) =>
    sectionRunnable(run, stage) ? (
      <button
        className="btn tiny"
        disabled={busy}
        onClick={() => queue.drain(stage, run)}
      >
        Run section
      </button>
    ) : null;

  const cellSection = (n, stage, title, note) => {
    const cells = byStage(stage);
    if (!cells.length) return null;
    const bySubQuestion = {};
    for (const c of cells) (bySubQuestion[c.sub_question_id] ??= []).push(c);

    return (
      <StageSection
        n={n}
        title={title}
        note={note}
        action={sectionAction(stage)}
        open={!done}
      >
        {Object.entries(bySubQuestion).map(([sqId, sqCells]) => (
          <div key={sqId} className="card">
            <Accordion
              defaultOpen
              className="sub-question"
              summary={
                <>
                  <b style={{ flex: "none" }}>{subQuestionLabel(sqId)}</b>
                  <span className="grow">{subQuestionById[sqId]?.question}</span>
                </>
              }
            >
              {sqCells.map((cell, i) => {
                // A modifier cell needs the base rate it moves, which lives in the
                // sibling step, not in its own payload.
                const baseStep =
                  stage === "inside_view"
                    ? stepFor("base_rates", cell.sub_question_id, cell.lens_name)
                    : cell;
                const lens =
                  baseStep?.payload?.lens ||
                  chosenLens(steps, cell.sub_question_id, cell.lens_name);
                const shared = {
                  step: cell,
                  lens,
                  researched: baseStep?.payload,
                  active: activeFor(cell),
                  busy,
                  onStart: start(cell),
                };
                return stage === "inside_view" ? (
                  <ModifierCard key={cell.id} {...shared} insidePayload={cell.payload} />
                ) : (
                  <BaseRateCard key={cell.id} {...shared} index={i} />
                );
              })}
            </Accordion>
          </div>
        ))}
      </StageSection>
    );
  };

  const synthesisSection = synthesisStep && (
    <StageSection
      n={5}
      title={STAGE_TITLES.synthesis}
      action={sectionAction("synthesis")}
      open
    >
      {activeFor(synthesisStep) ? (
        <div className="card">
          <div className="card-head">
            <span className="headline">
              <span className="spinner" /> Reflecting, synthesizing, critiquing…
            </span>
          </div>
          <CellActivity active={activeFor(synthesisStep)} />
        </div>
      ) : done && synthesisStep.payload ? (
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
            Arithmetic first (not agentic), then the synthesis agent adjusts within
            the configured slack and the checks critique it.
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
  );

  return (
    <div>
      <RunHeader
        run={run}
        queue={queue}
        busy={busy}
        onRunAll={runAll}
        onDelete={() => setConfirmingDelete(true)}
      />

      {confirmingDelete && (
        <ConfirmDialog
          title="Delete this forecast?"
          busy={deleting}
          error={deleteError}
          onCancel={() => {
            setConfirmingDelete(false);
            setDeleteError("");
          }}
          onConfirm={deleteRun}
        >
          <p>{run.question}</p>
          <p>
            {steps.filter((s) => s.status === "complete").length} of {steps.length}{" "}
            steps have run. Their reasoning — every lens, counted base rate, and
            adjustment — goes with the run and cannot be recovered.
          </p>
          {run.forecast_id && (
            <p className="card-sub">
              The saved forecast this run produced is kept, and stays scoreable.
            </p>
          )}
        </ConfirmDialog>
      )}

      {run.error && !busy && (
        <div className="error-banner">
          This run hit an error: {run.error}. Retry the failed step below.
        </div>
      )}

      {/* The answer leads once there is one. The badges keep their numbers, so the
          pipeline order stays readable even when section 5 is on top. */}
      {done && synthesisSection}

      <StageSection
        n={1}
        title={STAGE_TITLES.decompose}
        action={sectionAction("decompose")}
        open={!done}
      >
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
          ) : editing === decomposeStep.id ? (
            <DecomposeEditor
              payload={decomposition}
              saving={saving}
              error={saveError}
              onCancel={() => {
                setEditing(null);
                setSaveError("");
              }}
              onSave={(payload) => savePayload(decomposeStep.id, payload)}
            />
          ) : decomposition ? (
            <div className="card">
              <div className="card-head">
                <span className="headline">
                  {decomposition.sub_questions.length} sub-questions
                </span>
                {decomposeStep.edited_at && (
                  <span className="chip" title="A person wrote this payload">
                    edited
                  </span>
                )}
                <EditGate
                  step={decomposeStep}
                  steps={steps}
                  busy={busy}
                  onEdit={() => setEditing(decomposeStep.id)}
                />
              </div>
              {decomposition.sub_questions.map((s) => (
                <div key={s.id} className="evidence-row">
                  <b style={{ flex: "none" }}>{subQuestionLabel(s.id)}</b>
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
          action={sectionAction("lenses")}
          open={!done}
        >
          {byStage("lenses").map((step) => {
            const sq = subQuestionById[step.sub_question_id];
            const active = activeFor(step);
            if (editing === step.id) {
              return (
                <LensSetEditor
                  key={step.id}
                  payload={step.payload}
                  saving={saving}
                  error={saveError}
                  onCancel={() => {
                    setEditing(null);
                    setSaveError("");
                  }}
                  onSave={(payload) => savePayload(step.id, payload)}
                />
              );
            }
            return (
              <div key={step.id} className="card">
                <div className="card-head">
                  <span className="headline">
                    {subQuestionLabel(step.sub_question_id)} — {sq?.question}
                  </span>
                  {step.edited_at && (
                    <span className="chip" title="A person wrote this payload">
                      edited
                    </span>
                  )}
                  {step.status === "complete" && (
                    <EditGate
                      step={step}
                      steps={steps}
                      busy={busy}
                      onEdit={() => setEditing(step.id)}
                    />
                  )}
                </div>
                {sq?.rationale && <div className="card-sub">{sq.rationale}</div>}
                {active ? (
                  <CellActivity active={active} />
                ) : step.status === "complete" ? (
                  <div style={{ marginTop: 8 }}>
                    {(step.payload?.lenses || []).map((l, i) => (
                      <Accordion
                        key={l.name}
                        defaultOpen
                        summary={
                          <>
                            <b style={{ flex: "none" }}>{ordinal("Lens", i)}</b>
                            <span className="grow">{l.name}</span>
                            <span className="chip">w {l.weight}</span>
                          </>
                        }
                      >
                        {/* The only place `why_it_fits` and `weight_rationale` appear.
                            The measured cells restate the population and nothing else. */}
                        <Prose className="prose tight">{l.population}</Prose>
                        {l.why_it_fits && (
                          <div className="card-sub">
                            <Prose className="prose tight">{l.why_it_fits}</Prose>
                          </div>
                        )}
                        {l.weight_rationale && (
                          <div className="card-sub">
                            <b>Weight {l.weight}</b>{" "}
                            <Prose className="prose tight">{l.weight_rationale}</Prose>
                          </div>
                        )}
                      </Accordion>
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

      {!done && synthesisSection}
    </div>
  );
}

function chosenLens(steps, subQuestionId, lensName) {
  const lensesStep = steps.find(
    (s) => s.stage === "lenses" && s.sub_question_id === subQuestionId,
  );
  return (lensesStep?.payload?.lenses || []).find((l) => l.name === lensName);
}
