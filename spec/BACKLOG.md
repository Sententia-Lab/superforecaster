# Backlog

Broad features, one line each. No estimates, no owners, no status columns, no dates.

`Next` is roughly ordered; `Later` is not ordered at all. When an item is big enough to need a
design, it graduates to `spec/planned/specN.md` and the line here just points at it. Delete
lines freely — this file is a scratch list, not a record.

## Next

- **Editable review gates** — approve or edit a decomposition / lens set before advancing, instead of advance-only. Needs payload-edit endpoints + downstream invalidation.
- **Skip a stuck cell** — a permanently failing cell blocks its whole run today; retry is the only escape.
- **Eval harness** — `spec/planned/spec6.md`: cost accounting, replay cassettes, golden/platinum corpus. The system still has no measured accuracy number.

## Later

- **Run-to-completion button** — advance every remaining gate without clicking each one.
- **Lens editing in the UI** — add, remove, or re-weight a lens by hand.
- **Backtesting in the UI** — the `as_of` and model-garden clamps exist in code with no way to drive them from the browser.
- **Post-mortems beyond the CLI** — `run_postmortem` has no API route and no UI.

## Defects

- **Tests are not isolated from `backend/.env`** — a local `ADMIN_API_KEY` 401s 10 admin-route tests; CI never sees it. Workaround: `ADMIN_API_KEY="" uv run pytest`.
- **`GoldenQuestion` / `QuestionScore` / `Scorecard` are defined but unused** — dead until the eval corpus lands.
- **`spec/planned/` holds both spec4 and spec6** — spec6 supersedes spec4; one of them should move or be marked.
