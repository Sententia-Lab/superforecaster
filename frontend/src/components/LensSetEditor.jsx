import { useState } from "react";
import { normalizeWeights, weightSum } from "../derive.js";
import EditorField from "./EditorField.jsx";

const blank = () => ({
  name: "",
  population: "",
  why_it_fits: "",
  weight: 0.5,
  weight_rationale: "",
});

/**
 * Correct one sub-question's lens set before any of its rates are measured.
 *
 * Weights must sum to exactly 1.00. The agent's own output is rescaled on the way in, but
 * a hand-written set is rejected instead — silently rewriting numbers somebody typed would
 * hide the constraint rather than teach it. Normalize is the deliberate version of that
 * rewrite, on a button the user presses.
 *
 * The whole set locks together once any of its cells has been researched, because weights
 * are shares: changing one changes another, and re-weighting a measured lens is what
 * ADR 40 exists to prevent.
 */
export default function LensSetEditor({ payload, onSave, onCancel, saving, error }) {
  const [lenses, setLenses] = useState(() =>
    (payload?.lenses || []).map((l) => ({ ...l })),
  );

  const patch = (i, field, value) =>
    setLenses((ls) => ls.map((l, j) => (j === i ? { ...l, [field]: value } : l)));

  const sum = weightSum(lenses);
  const balanced = sum === 1;
  const incomplete = lenses.some(
    (l) => !l.name.trim() || !l.population.trim() || !l.why_it_fits.trim(),
  );
  const duplicate =
    new Set(lenses.map((l) => l.name.trim())).size !== lenses.length;

  return (
    <div className="card editor">
      {lenses.map((l, i) => (
        <div key={i} className="editor-block">
          <div className="editor-block-head">
            <b className="editor-block-id">Lens {i + 1}</b>
            <label className="field-inline" title="Relevance only, never sample size">
              <span className="editor-hint">weight</span>
              <input
                className="field-input tiny-input"
                type="number"
                min="0.01"
                max="1"
                step="0.01"
                value={l.weight}
                onChange={(e) => patch(i, "weight", Number(e.target.value))}
              />
            </label>
            <span className="editor-spacer" />
            <button
              className="btn tiny ghost"
              title="Remove this lens"
              disabled={lenses.length <= 1}
              onClick={() => setLenses((ls) => ls.filter((_, j) => j !== i))}
            >
              ✕
            </button>
          </div>

          <EditorField
            label="Name"
            hint="short label, unique within this sub-question"
            singleLine
            value={l.name}
            onChange={(v) => patch(i, "name", v)}
          />
          <EditorField
            label="Population"
            hint="precise enough that someone else could count the same cases"
            value={l.population}
            onChange={(v) => patch(i, "population", v)}
          />
          <EditorField
            label="Why it fits"
            hint="why cases from this population tell you about THIS sub-question"
            value={l.why_it_fits}
            onChange={(v) => patch(i, "why_it_fits", v)}
          />
          <EditorField
            label="Weight rationale"
            hint="nothing else in the pipeline can check this number"
            value={l.weight_rationale}
            onChange={(v) => patch(i, "weight_rationale", v)}
          />
        </div>
      ))}

      <div className="form-actions">
        <button
          className="btn tiny"
          disabled={lenses.length >= 3}
          onClick={() => setLenses((ls) => [...ls, blank()])}
        >
          Add lens
        </button>
        <span className={`chip sigma${balanced ? " green" : " red"}`}>
          Σ {sum.toFixed(2)}
        </span>
        {!balanced && (
          <button
            className="btn tiny"
            onClick={() => setLenses((ls) => normalizeWeights(ls))}
          >
            Normalize
          </button>
        )}
        <span className="hint">{lenses.length} of 1–3</span>
      </div>

      {duplicate && (
        <div className="error-banner">
          Lens names must be unique — a lens is identified by (sub-question, name).
        </div>
      )}
      {error && <div className="error-banner">{error}</div>}

      <div className="form-actions">
        <button
          className="btn tiny primary"
          disabled={saving || !balanced || incomplete || duplicate}
          onClick={() => onSave({ lenses })}
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
