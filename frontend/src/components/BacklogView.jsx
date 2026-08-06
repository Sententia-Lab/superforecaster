import { useEffect, useState } from "react";
import { api } from "../api.js";
import FieldEditor, { isComplete } from "./FieldEditor.jsx";

/** A forecast that hasn't been run yet: editable fields, Start, Remove. */
export default function BacklogView({ runId, onChanged, onDeleted }) {
  const [fields, setFields] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .getRun(runId)
      .then((run) =>
        setFields({
          question: run.question,
          resolution_criteria: run.resolution_criteria,
          resolution_date: run.resolution_date || "",
          resolution_source: run.resolution_source,
          category: run.category,
        }),
      )
      .catch((e) => setError(e.message));
  }, [runId]);

  if (!fields) return error ? <div className="error-banner">{error}</div> : null;

  const saveAnd = async (fn) => {
    setBusy(true);
    setError("");
    try {
      await api.editRun(runId, fields);
      await fn?.();
      onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h1 className="qtitle">Backlogged forecast</h1>
      <div className="qmeta">
        Not run yet. Edit the fields, then start when all four are present.
      </div>
      {error && <div className="error-banner">{error}</div>}
      <FieldEditor fields={fields} onChange={setFields} />
      <div className="form-actions">
        <button
          className="btn primary"
          disabled={!isComplete(fields) || busy}
          onClick={() => saveAnd(() => api.startRun(runId))}
        >
          Start forecast
        </button>
        <button className="btn" disabled={busy} onClick={() => saveAnd()}>
          Save
        </button>
        <button
          className="btn danger"
          disabled={busy}
          onClick={async () => {
            await api.deleteRun(runId);
            onDeleted();
          }}
        >
          Remove
        </button>
      </div>
    </div>
  );
}
