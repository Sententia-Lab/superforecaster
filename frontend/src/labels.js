// Display labels, computed from position at render time. ADR 59.
//
// `sq1` is the stored id and part of the step's unique key. "Sub-question 1" is what a
// reader sees, and it is not stored anywhere — a stored label drifts the moment a list is
// reordered, and position is already the answer.

const SUB_QUESTION_ID = /^sq(\d+)$/;

/** `sq1` -> `Sub-question 1`. An id in any other shape is returned as it came. */
export function subQuestionLabel(id) {
  const m = SUB_QUESTION_ID.exec(id || "");
  return m ? `Sub-question ${m[1]}` : id || "";
}

// How strongly a group of sub-questions moves together. `w` mirrors `models.DEPENDENCE`
// and is shown so a reader can see what the label costs, not just what it is called.
// Kept here rather than derived from the payload: the payload stores only the kind, and a
// reader looking at a saved run should still be told what that kind meant.
export const DEPENDENCE_KINDS = [
  {
    value: "none",
    label: "independent",
    w: 0.0,
    description: "Nothing links them. The rates multiply as they always did.",
  },
  {
    value: "shared_driver",
    label: "shared driver",
    w: 0.35,
    description: "One force moves both. Neither causes the other.",
  },
  {
    value: "one_causes_other",
    label: "one causes the other",
    w: 0.5,
    description: "One of them happening makes the other more likely.",
  },
];

/** A kind's display entry. An unknown kind falls back to `none` rather than blanking. */
export function dependenceKind(value) {
  return DEPENDENCE_KINDS.find((k) => k.value === value) || DEPENDENCE_KINDS[0];
}

/** `ordinal("Lens", 0)` -> `Lens 1`. Lenses, base rates and modifiers have no ids. */
export function ordinal(prefix, index) {
  return `${prefix} ${index + 1}`;
}

/**
 * The first sentence of `text`, for a modifier written before `Adjustment.title` existed.
 * Falls back to the whole string when there is no sentence break to find.
 */
export function firstSentence(text, max = 80) {
  const s = (text || "").trim();
  if (!s) return "";
  const end = s.search(/[.!?](\s|$)/);
  const cut = end > 0 && end < max ? s.slice(0, end) : s.slice(0, max);
  return cut.length < s.length ? `${cut}…` : cut;
}
