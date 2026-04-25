# Claude Instructions — Superforecaster

## Spec-Driven Development

This project uses spec-driven development. Before implementing anything:
1. Read `spec/SPEC.md` to understand the current phase and requirements.
2. Read `spec/CURRENT_STATE.md` to understand what already exists.
3. Read `spec/TECHNICAL_DIRECTION.md` to understand architecture decisions before making new ones.

Do not introduce architecture patterns or dependencies that conflict with `spec/TECHNICAL_DIRECTION.md` without flagging the conflict and getting explicit approval.

---

## Keeping CURRENT_STATE.md Up to Date

After any session where you add, remove, or change code, update `spec/CURRENT_STATE.md` to reflect reality. This is not optional — it is part of completing any task.

Specifically, update it when:
- A new file, module, or package is created or deleted
- A new feature is implemented or an existing one is removed
- A known bug is fixed (remove it from "Known Issues")
- A new dependency is added to `pyproject.toml`
- A new environment variable is required
- A deployment asset is added or changed (database, API server, frontend build)
- A spec phase from `spec/SPEC.md` is completed

**What to update:**
- Repository layout (if structure changed)
- Data models section (if models changed)
- Tools section (if tools changed)
- Core functions (if functions added/removed/renamed)
- What Actually Works / Known Issues (accurate current status)
- Dependencies table (if pyproject.toml changed)
- Environment variables (if .env changed)
- Deployment Assets (if anything is now deployed or hosted)

Keep entries factual and brief. Do not describe what you plan to do — only what exists and works right now.

---

## Superforecasting Methodology

The agent must implement the methodology in `spec/superforecasting_methodology.md`. When writing or modifying agent prompts, cross-reference that document. The 16 principles are the spec for agent behavior — not suggestions.

---

## Code Style

- Python ≥ 3.12, managed with `uv`
- Pydantic AI for the agent framework
- Pydantic v2 for all data models
- No comments explaining what code does — only comments for non-obvious why
- Run `uv run pytest` before marking any task complete
