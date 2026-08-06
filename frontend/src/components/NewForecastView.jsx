import { useState } from "react";
import { api } from "../api.js";
import FieldEditor, { isComplete } from "./FieldEditor.jsx";

/**
 * Freeform text → AI-suggested forecast fields → run or backlog.
 *
 * The AI draft fills all four required fields, so "Run now" is immediately live for
 * it; hand-typed fields stay gated until question, criteria, date, and source are
 * all present. Partial drafts can always be parked in the backlog.
 */
export default function NewForecastView({ onCreated }) {
  const [text, setText] = useState("");
  const [phase, setPhase] = useState("draft"); // draft | parsing | review
  const [fields, setFields] = useState({});
  const [critique, setCritique] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const onDraft = async () => {
    setPhase("parsing");
    setError("");
    try {
      const { parsed, critique } = await api.draftQuestion(text);
      setFields({
        question: parsed.question,
        resolution_criteria: parsed.resolution_criteria,
        resolution_date: parsed.resolution_date,
        resolution_source: parsed.resolution_source,
        category: parsed.category || "general",
      });
      setCritique(critique);
      setPhase("review");
    } catch (e) {
      setError(e.message);
      setPhase("draft");
    }
  };

  const save = async (start) => {
    setBusy(true);
    setError("");
    try {
      const run = await api.createRun(fields);
      if (start) await api.startRun(run.id);
      onCreated(run, start);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (phase === "draft" || phase === "parsing") {
    return (
      <div>
        <h1 className="qtitle">New forecast</h1>
        {error && <div className="error-banner">{error}</div>}
        <div className="field">
          <label>Describe what you want forecast</label>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Type the forecast in your own words — the AI will phrase it as a resolvable question with criteria, a date, and a source."
          />
        </div>
        <div className="form-actions">
          <button
            className="btn primary"
            disabled={text.trim().length < 20 || phase === "parsing"}
            onClick={onDraft}
          >
            {phase === "parsing" ? (
              <>
                <span className="spinner" /> Drafting…
              </>
            ) : (
              "Draft with AI"
            )}
          </button>
          <button
            className="btn ghost"
            onClick={() => {
              setFields({ question: text.trim() });
              setCritique(null);
              setPhase("review");
            }}
          >
            Skip — fill fields myself
          </button>
        </div>
      </div>
    );
  }

  const complete = isComplete(fields);
  return (
    <div>
      <h1 className="qtitle">Review the forecast</h1>
      {error && <div className="error-banner">{error}</div>}
      {critique &&
      (!critique.is_resolvable ||
        critique.ambiguities?.length ||
        critique.missing?.length) ? (
        <div className="critique">
          <b>Critique (P3 — resolvability):</b>{" "}
          {[...(critique.ambiguities || []), ...(critique.missing || [])].join(
            " · ",
          ) || "the criteria could not be adjudicated as written"}
          <div style={{ marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap" }}>
            {critique.suggested_criteria ? (
              <button
                className="btn tiny"
                onClick={() =>
                  setFields({
                    ...fields,
                    resolution_criteria: critique.suggested_criteria,
                  })
                }
              >
                Apply suggested criteria
              </button>
            ) : null}
            {critique.suggested_resolution_source ? (
              <button
                className="btn tiny"
                onClick={() =>
                  setFields({
                    ...fields,
                    resolution_source: critique.suggested_resolution_source,
                  })
                }
              >
                Use source: {critique.suggested_resolution_source}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
      <FieldEditor fields={fields} onChange={setFields} />
      <div className="form-actions">
        <button
          className="btn primary"
          disabled={!complete || busy}
          onClick={() => save(true)}
        >
          Run now
        </button>
        <button className="btn" disabled={busy} onClick={() => save(false)}>
          Add to backlog
        </button>
        <button className="btn ghost" onClick={() => setPhase("draft")}>
          Back
        </button>
        {!complete && (
          <span className="hint">
            Running needs a phrased question, criteria, date, and source.
          </span>
        )}
      </div>
    </div>
  );
}
