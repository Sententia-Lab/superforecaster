import { useEffect, useRef } from "react";

/**
 * A modal that asks before something irreversible happens.
 *
 * Cancel is what the dialog focuses, and Escape or a click outside both take it, so the
 * safe answer is the one you get by reflex. The caller supplies the body, because "are you
 * sure" on its own tells nobody what they are about to lose.
 */
export default function ConfirmDialog({
  title,
  children,
  confirmLabel = "Delete",
  onConfirm,
  onCancel,
  busy,
  error,
}) {
  const cancelRef = useRef(null);

  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title">{title}</div>
        <div className="modal-body">{children}</div>
        {error && <div className="error-banner">{error}</div>}
        <div className="form-actions modal-actions">
          <button
            ref={cancelRef}
            className="btn"
            disabled={busy}
            onClick={onCancel}
          >
            Cancel
          </button>
          <button className="btn danger" disabled={busy} onClick={onConfirm}>
            {busy ? "Deleting…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
