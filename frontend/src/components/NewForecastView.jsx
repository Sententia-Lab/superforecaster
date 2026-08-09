import { useState } from "react";
import { api } from "../api.js";
import FieldEditor, { isComplete } from "./FieldEditor.jsx";

/**
 * Freeform text → AI-drafted forecast fields → run or backlog.
 *
 * "Check resolvable" is a rewrite, not a report: the AI's criteria and source replace
 * what is in the fields, and one sentence says what it changed. The old text is one
 * undo away in the editor, so there is nothing to accept or dismiss.
 *
 * Hand-typed fields stay gated until question, criteria, date, and source are all
 * present. Partial drafts can always be parked in the backlog.
 */
export default function NewForecastView({ onCreated }) {
  const [text, setText] = useState("");
  const [phase, setPhase] = useState("draft"); // draft | parsing | review
  const [fields, setFields] = useState({});
  const [whatChanged, setWhatChanged] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);

  const onDraft = async () => {
    setPhase("parsing");
    setError("");
    try {
      const parsed = await api.draftQuestion(text);
      setFields({
        question: parsed.question,
        resolution_criteria: parsed.resolution_criteria,
        resolution_date: parsed.resolution_date,
        resolution_source: parsed.resolution_source,
        category: parsed.category || "general",
      });
      setWhatChanged("");
      setPhase("review");
    } catch (e) {
      setError(e.message);
      setPhase("draft");
    }
  };

  const onCheck = async () => {
    setChecking(true);
    setError("");
    try {
      const c = await api.critiqueQuestion(fields);
      setFields({
        ...fields,
        resolution_criteria: c.suggested_criteria || fields.resolution_criteria,
        resolution_source:
          c.suggested_resolution_source || fields.resolution_source,
      });
      setWhatChanged(
        c.what_changed || "Nothing to change — this resolves as written.",
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setChecking(false);
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
              setWhatChanged("");
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
  const checkable = Boolean(
    (fields.question || "").trim() && (fields.resolution_criteria || "").trim(),
  );
  return (
    <div>
      <h1 className="qtitle">Review the forecast</h1>
      {error && <div className="error-banner">{error}</div>}
      <FieldEditor fields={fields} onChange={setFields} />
      <div className="form-actions">
        <button
          className="btn"
          disabled={!checkable || checking}
          onClick={onCheck}
        >
          {checking ? (
            <>
              <span className="spinner" /> Checking…
            </>
          ) : (
            "Check resolvable"
          )}
        </button>
        {!checkable && (
          <span className="hint">
            Checking needs a question and criteria to rewrite.
          </span>
        )}
      </div>
      {whatChanged && <div className="critique">{whatChanged}</div>}
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
