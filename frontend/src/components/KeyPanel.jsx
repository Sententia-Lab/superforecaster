import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";

// The server-held keys, in the order they matter to a run.
const FIELDS = [
  {
    field: "llm_api_key",
    origin: "llm",
    label: "LLM API key",
    // The server names the variable, because a gateway install and a direct Anthropic
    // install are credentialed by different ones.
    hint: (config) =>
      `${config?.keys?.llm_var || "ANTHROPIC_API_KEY"} — the model. Nothing runs ` +
      `without it.`,
  },
  {
    field: "tavily_api_key",
    origin: "tavily",
    label: "Tavily API key",
    hint:
      "TAVILY_API_KEY — web search. Without it the agents fall back to Wikipedia " +
      "alone, and the reference classes they find are noticeably thinner.",
  },
  {
    field: "wikipedia_api_key",
    origin: "wikipedia",
    label: "Wikipedia API key",
    hint: "WIKIPEDIA_API_KEY — optional. Raises the rate limit; nothing needs it.",
  },
];

const TONE = { environment: "green", ".env": "green", session: "yellow" };
const WORDING = {
  environment: "from environment",
  ".env": "from .env",
  session: "set this session",
  unset: "unset",
};

function OriginChip({ origin }) {
  return <span className={`chip ${TONE[origin] || ""}`}>{WORDING[origin] || origin}</span>;
}

/**
 * Every key the app needs, in one place.
 *
 * The server-held keys are write-only (ADR 61): no route hands a value back, so the
 * inputs always render empty and the chip carries the state. A blank field on save means
 * "leave it alone" — clearing is a separate, deliberate button.
 */
export default function KeyPanel({ config, busy, onSaved, onClose }) {
  const [values, setValues] = useState({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const firstRef = useRef(null);

  useEffect(() => {
    firstRef.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const origins = config?.keys || {};
  const set = (field, value) => setValues((v) => ({ ...v, [field]: value }));

  /**
   * `clear` is the Clear button's field name — it sends `""`, the one way to unset a key.
   * Everything else comes from what was typed, and a blank input is left out entirely, so
   * saving one key never disturbs another.
   */
  const save = async (clear = null) => {
    setSaving(true);
    setError("");
    try {
      const body = {};
      for (const { field } of FIELDS) {
        const typed = (values[field] || "").trim();
        if (typed) body[field] = typed;
      }
      if (clear) body[clear] = "";

      if (Object.keys(body).length) onSaved(await api.setKeys(body));
      setValues({});
      if (!clear) onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Keys"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title">Keys</div>
        <div className="modal-body">
          {FIELDS.map(({ field, origin, label, hint }, i) => (
            <div className="editor-field" key={field}>
              <div className="editor-label">
                {label}
                <OriginChip origin={origins[origin] || "unset"} />
                {origins[origin] && origins[origin] !== "unset" && (
                  <button
                    className="btn tiny ghost"
                    disabled={saving || busy}
                    onClick={() => save(field)}
                  >
                    Clear
                  </button>
                )}
              </div>
              <input
                ref={i === 0 ? firstRef : undefined}
                className="field-input"
                type="password"
                autoComplete="off"
                placeholder="Leave blank to keep the current value"
                value={values[field] || ""}
                onChange={(e) => set(field, e.target.value)}
              />
              <div className="editor-hint">
                {typeof hint === "function" ? hint(config) : hint}
              </div>
            </div>
          ))}

          <div className="card-sub">
            These keys live in the server process and are dropped when it restarts. Put
            them in backend/.env to make them permanent. A key set here is used by every
            run on this server.
          </div>
        </div>

        {error && <div className="error-banner">{error}</div>}
        {busy && (
          <div className="hint">
            A step is streaming. Changing the LLM key now would apply mid-run.
          </div>
        )}

        <div className="form-actions modal-actions">
          <button className="btn" disabled={saving} onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn primary"
            disabled={saving || busy}
            onClick={() => save()}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
