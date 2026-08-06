// The four-field editor shared by NewForecastView and BacklogView.
//
// The gate is visible, not just enforced: a missing required field gets a red
// border, and the caller decides which actions the gate disables.

export const REQUIRED = [
  ["question", "Question"],
  ["resolution_criteria", "Resolution criteria"],
  ["resolution_date", "Resolution date"],
  ["resolution_source", "Resolution source"],
];

export function isComplete(fields) {
  return REQUIRED.every(([key]) => (fields[key] || "").trim());
}

export default function FieldEditor({ fields, onChange }) {
  const set = (key) => (e) => onChange({ ...fields, [key]: e.target.value });
  const missing = (key) => !(fields[key] || "").trim();

  return (
    <>
      <div className="field">
        <label>Question</label>
        <textarea
          data-testid="f-question"
          className={missing("question") ? "missing" : ""}
          value={fields.question || ""}
          onChange={set("question")}
          placeholder="Will X happen by Y?"
          style={{ minHeight: 56 }}
        />
      </div>
      <div className="field">
        <label>Resolution criteria</label>
        <textarea
          className={missing("resolution_criteria") ? "missing" : ""}
          value={fields.resolution_criteria || ""}
          onChange={set("resolution_criteria")}
          placeholder="The exact observable that settles it."
          style={{ minHeight: 56 }}
        />
      </div>
      <div className="field">
        <label>Resolution date</label>
        <input
          type="date"
          className={missing("resolution_date") ? "missing" : ""}
          value={(fields.resolution_date || "").slice(0, 10)}
          onChange={set("resolution_date")}
        />
      </div>
      <div className="field">
        <label>Resolution source</label>
        <input
          className={missing("resolution_source") ? "missing" : ""}
          value={fields.resolution_source || ""}
          onChange={set("resolution_source")}
          placeholder="Who adjudicates this — the publication, dataset, or body."
        />
      </div>
    </>
  );
}
