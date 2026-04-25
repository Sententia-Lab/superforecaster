# /update-direction

Updates `spec/TECHNICAL_DIRECTION.md` to capture new architecture decisions, desired features, or direction changes made by the technical lead.

## When to Use

Run this after:
- Deciding on a new architecture pattern or technology
- Ruling something out of scope
- Changing a previously stated position
- Adding a new desired feature with a clear rationale
- Completing a spec phase and wanting to record what was learned

## Process

1. Read `spec/TECHNICAL_DIRECTION.md` to understand the current state.
2. Read `spec/CURRENT_STATE.md` to understand what's been built.
3. Ask the user: **"What changed or what have you decided?"** — one question, wait for their answer.
4. If the answer touches an existing section, update that section in place. If it's genuinely new, add it in the right place.
5. Do not rewrite sections that weren't mentioned. Preserve the voice and framing of the document.
6. After writing, tell the user exactly what changed (section name + one-line summary of the change).

## Rules for Editing TECHNICAL_DIRECTION.md

- This document reflects deliberate decisions by the technical lead — not implementation details.
- Write in plain language. Avoid code snippets unless they illustrate a decision boundary.
- Every decision entry must have a **Rationale** — the why, not just the what.
- If a decision reverses a previous one, remove or update the old entry rather than appending a correction.
- "Out of Scope" section should be updated when something is explicitly ruled out or ruled back in.
- Do not add speculative future features — only decisions that have been made.
