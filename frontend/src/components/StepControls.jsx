/**
 * The gate button for one step: Run / Retry (with an optional deeper budget when
 * the failure was a budget overrun). Disabled while any other step streams —
 * mirroring the server's one-in-flight rule instead of discovering it as a 409.
 */
export default function StepControls({ step, label, busy, onStart, error }) {
  const failed = step.status === "error";
  const budgetFailure = /budget|max_iterations|UsageLimit/i.test(step.error || "");
  return (
    <div>
      {(failed || error) && (
        <div className="error-banner" style={{ marginTop: 8 }}>
          {error || step.error}
        </div>
      )}
      <div className="form-actions" style={{ marginTop: 8 }}>
        <button
          className={`btn tiny${failed ? "" : " primary"}`}
          disabled={busy}
          onClick={() => onStart()}
        >
          {failed ? "Retry" : label}
        </button>
        {failed && budgetFailure && (
          <button
            className="btn tiny"
            disabled={busy}
            onClick={() => onStart({ deeper: true })}
          >
            Retry with 2× search budget
          </button>
        )}
      </div>
    </div>
  );
}
