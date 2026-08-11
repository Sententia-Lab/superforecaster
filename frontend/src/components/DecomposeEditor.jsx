import { useState } from "react";
import EditorField from "./EditorField.jsx";
import { DEPENDENCE_KINDS, dependenceKind } from "../labels.js";

const CHAIN_RULES = ["conjunction", "disjunction", "custom"];

const blank = () => ({
  question: "",
  rationale: "",
  knowability: "researchable",
  probability: 0.5,
  group: -1,
});

const blankGroup = () => ({ name: "", kind: "shared_driver" });

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
 *
 * A group names its members by position too, for the same reason, so the two cannot
 * disagree. A group is its own object here and a row points at one by index, rather than
 * rows agreeing on a name: a group needs a description and a dependence kind of its own,
 * and a name doing double duty as the join key cannot be edited without unlinking its
 * members. That shape also makes two of the server's three rules unreachable — a row holds
 * one index, so nothing joins two groups, and a group under two members is dropped on save.
 */
export default function DecomposeEditor({ payload, onSave, onCancel, saving, error }) {
  const [claims, setClaims] = useState(() =>
    (payload?.sub_questions || []).map((s, i) => ({
      ...s,
      group: (payload?.dependent_groups || []).findIndex((g) =>
        (g.members || []).includes(i + 1),
      ),
    })),
  );
  const [chainRule, setChainRule] = useState(payload?.chain_rule || "conjunction");
  const [chainNote, setChainNote] = useState(payload?.chain_note || "");
  const [groups, setGroups] = useState(() =>
    (payload?.dependent_groups || []).map((g) => ({ name: g.name, kind: g.kind })),
  );

  const patch = (i, field, value) =>
    setClaims((cs) => cs.map((c, j) => (j === i ? { ...c, [field]: value } : c)));

  const patchGroup = (gi, field, value) =>
    setGroups((gs) => gs.map((g, j) => (j === gi ? { ...g, [field]: value } : g)));

  // Removing a group unlinks its members and shifts every later index down, so a row
  // never points at the wrong group or at one that is gone.
  const removeGroup = (gi) => {
    setGroups((gs) => gs.filter((_, j) => j !== gi));
    setClaims((cs) =>
      cs.map((c) => ({
        ...c,
        group: c.group === gi ? -1 : c.group > gi ? c.group - 1 : c.group,
      })),
    );
  };

  const membersOf = (gi) => claims.flatMap((c, i) => (c.group === gi ? [i + 1] : []));
  const groupLabel = (g, gi) => g.name.trim() || `Group ${gi + 1}`;

  const tooFew = claims.length < 3;
  const incomplete = claims.some((c) => !c.question.trim() || !c.rationale.trim());

  // `custom` has no formula for a dependence parameter to move, and the server rejects the
  // combination, so the grouping controls disappear rather than saving something invalid.
  const grouping = chainRule !== "custom";

  // A group of one has nothing to correlate with, so it is not a group.
  const sent = groups
    .map((g, gi) => ({ name: groupLabel(g, gi), members: membersOf(gi), kind: g.kind }))
    .filter((g) => g.members.length > 1);

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
            {grouping && groups.length > 0 && (
              <label className="field-inline" title="Which group this sub-question is in.">
                <span className="editor-hint">moves with</span>
                <select
                  className="field-input narrow"
                  value={c.group}
                  onChange={(e) => patch(i, "group", Number(e.target.value))}
                >
                  <option value={-1}>nothing</option>
                  {groups.map((g, gi) => (
                    <option key={gi} value={gi}>
                      {groupLabel(g, gi)}
                    </option>
                  ))}
                </select>
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

      {grouping && (
        <>
          {groups.map((g, gi) => {
            const members = membersOf(gi);
            const kind = dependenceKind(g.kind);
            return (
              <div key={gi} className="editor-block">
                <div className="editor-block-head">
                  <b className="editor-block-id">{groupLabel(g, gi)}</b>
                  <select
                    className="field-input narrow"
                    title="How strongly these move together."
                    value={g.kind}
                    onChange={(e) => patchGroup(gi, "kind", e.target.value)}
                  >
                    {DEPENDENCE_KINDS.map((k) => (
                      <option key={k.value} value={k.value}>
                        {k.label}
                      </option>
                    ))}
                  </select>
                  <span className="chip" title="Dependence parameter">
                    {kind.w.toFixed(2)}
                  </span>
                  <span className="editor-spacer" />
                  <button
                    className="btn tiny ghost"
                    title="Remove this group"
                    onClick={() => removeGroup(gi)}
                  >
                    ✕
                  </button>
                </div>

                <EditorField
                  label="What they share"
                  hint="the force behind both, or the causal path"
                  value={g.name}
                  onChange={(v) => patchGroup(gi, "name", v)}
                />

                <div className="src-chips">
                  {members.length ? (
                    members.map((m) => (
                      <span key={m} className="src-chip">
                        sq{m}
                      </span>
                    ))
                  ) : (
                    <span className="hint">
                      No members yet — set “moves with” on two sub-questions above.
                    </span>
                  )}
                </div>
                <div className="hint" style={{ marginTop: 4 }}>
                  {kind.description}
                  {members.length < 2 &&
                    " This group has fewer than two members, so it will be dropped on save."}
                </div>
              </div>
            );
          })}

          <div className="form-actions">
            <button
              className="btn tiny"
              disabled={groups.length >= Math.floor(claims.length / 2)}
              onClick={() => setGroups((gs) => [...gs, blankGroup()])}
            >
              Add group
            </button>
            <span className="hint">
              Group sub-questions that do not move independently. Grouped rates do not
              simply multiply.
            </span>
          </div>
        </>
      )}

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
              dependent_groups: grouping ? sent : [],
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
