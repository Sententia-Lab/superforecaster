import { useState } from "react";
import EditorField from "./EditorField.jsx";

const CHAIN_RULES = ["conjunction", "disjunction", "custom"];

const blank = () => ({
  question: "",
  rationale: "",
  knowability: "researchable",
  probability: 0.5,
});

/**
 * Correct a decomposition before anything is researched against it.
 *
 * `probability` appears only on judgment rows. `checks.chain_inputs` reads a sub-question's
 * own probability only when nothing researched it: a researchable sub-question takes its
 * rate from its lenses, so a number typed here would be discarded, while a judgment
 * sub-question has no lenses and this is its only contribution to the anchor. Leaving it
 * out entirely would make a conjunction treat that column as 1.0.
 *
 * The `sqN` shown on each block is live, not stored. The server re-stamps ids by position
 * on save, so removing the second sub-question renumbers everything below it — showing
 * that as you edit is more honest than showing the ids that are about to change.
 */
export default function DecomposeEditor({ payload, onSave, onCancel, saving, error }) {
  const [claims, setClaims] = useState(() =>
    (payload?.sub_questions || []).map((s) => ({ ...s })),
  );
  const [chainRule, setChainRule] = useState(payload?.chain_rule || "conjunction");
  const [chainNote, setChainNote] = useState(payload?.chain_note || "");

  const patch = (i, field, value) =>
    setClaims((cs) => cs.map((c, j) => (j === i ? { ...c, [field]: value } : c)));

  const tooFew = claims.length < 3;
  const incomplete = claims.some((c) => !c.question.trim() || !c.rationale.trim());

  return (
    <div className="card editor">
      {claims.map((c, i) => (
        <div key={i} className="editor-block">
          <div className="editor-block-head">
            <b className="editor-block-id">sq{i + 1}</b>
            <select
              className="field-input narrow"
              value={c.knowability}
              onChange={(e) => patch(i, "knowability", e.target.value)}
            >
              <option value="researchable">researchable</option>
              <option value="judgment">judgment</option>
            </select>
            {c.knowability === "judgment" && (
              <label
                className="field-inline"
                title="Used directly in the chain. Nothing measures a judgment sub-question."
              >
                <span className="editor-hint">working estimate</span>
                <input
                  className="field-input tiny-input"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  value={c.probability}
                  onChange={(e) => patch(i, "probability", Number(e.target.value))}
                />
              </label>
            )}
            <span className="editor-spacer" />
            <button
              className="btn tiny ghost"
              title="Remove this sub-question"
              disabled={claims.length <= 3}
              onClick={() => setClaims((cs) => cs.filter((_, j) => j !== i))}
            >
              ✕
            </button>
          </div>

          <EditorField
            label="Sub-question"
            hint="specific enough to argue about separately"
            value={c.question}
            onChange={(v) => patch(i, "question", v)}
          />
          <EditorField
            label="Rationale"
            value={c.rationale}
            onChange={(v) => patch(i, "rationale", v)}
          />
        </div>
      ))}

      <div className="form-actions">
        <button
          className="btn tiny"
          disabled={claims.length >= 5}
          onClick={() => setClaims((cs) => [...cs, blank()])}
        >
          Add sub-question
        </button>
        <span className="hint">{claims.length} of 3–5</span>
      </div>

      <div className="editor-block">
        <div className="editor-block-head">
          <b className="editor-block-id">Chain</b>
          <select
            className="field-input narrow"
            value={chainRule}
            onChange={(e) => setChainRule(e.target.value)}
          >
            {CHAIN_RULES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
        <EditorField
          label="How they combine"
          hint="in prose"
          value={chainNote}
          onChange={setChainNote}
        />
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="form-actions">
        <button
          className="btn tiny primary"
          disabled={saving || tooFew || incomplete}
          onClick={() =>
            onSave({
              sub_questions: claims.map((c) => ({
                question: c.question,
                rationale: c.rationale,
                knowability: c.knowability,
                // Required by `SubPrediction` for every row. Discarded for researchable
                // ones, where the lenses supply the rate.
                probability: Number(c.probability) || 0.5,
              })),
              chain_rule: chainRule,
              chain_note: chainNote,
            })
          }
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button className="btn tiny ghost" disabled={saving} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
