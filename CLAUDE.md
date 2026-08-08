# Claude Instructions — Superforecaster

## Writing Style (applies to all output)

Use ASD-STE100 Simplified Technical English.

- One idea per sentence. Keep sentences short.
- Use the active voice.
- Give every sentence a descriptive subject. Do not open a sentence with "this", "that", or "there" on its own.
- Define a term the first time you use it. Do not invent new terms.
- Do not use an acronym unless it is standard in the industry.
- State an idea directly. Do not introduce it with a short jargon phrase first.

Do not use these phrases:

- "smoking gun"
- "load-bearing"
- "circuit breaker"
- "the honest caveat"
- "blast radius"
- "the ___ that lands" (any variation)
- "the ___ headline" (any variation)

The style rules cover prose, comments, docstrings, specs, and commit messages.

---

## Spec-Driven Development

This project uses spec-driven development. Before implementing anything:
1. Read `spec/planned/` for work being designed, and `spec/implemented/` for what has shipped.
2. Read `spec/CURRENT_STATE.md` to understand what already exists.
3. Read `spec/ADR.md` to understand architecture decisions before making new ones.

The backlog lives in the **GitHub Project**, never in a file in this repo:
https://github.com/orgs/Sententia-Lab/projects/5

```bash
gh project item-list 5 --owner Sententia-Lab          # read
gh project item-create 5 --owner Sententia-Lab --title "..." --body "one sentence"
```

- Broad features, one line each. No estimates, no owners, no dates. Built-in Status
  (Todo / In Progress / Done) is the whole workflow; leave `Priority`, `Size`, and
  `Estimate` empty.
- Draft issues by default, so the repo issue tracker stays quiet until an item is picked up.
- An item earns a real description only when it graduates to `spec/planned/specN.md`.
- Delete items freely rather than archiving them into a column nobody reads.

Do not introduce architecture patterns or dependencies that conflict with `spec/ADR.md` without flagging the conflict and getting explicit approval. When a decision is reversed, supersede the ADR entry rather than deleting it — the history is the useful part.

---

## Writing a Spec

### Where the file goes

```
spec/
  planned/       # specs that are not built yet
  implemented/   # move the spec here after the code ships
  CURRENT_STATE.md
  ADR.md
```

Write the plan to `spec/planned/specN.md`, where `N` is the next free number.
Move the same file to `spec/implemented/` when the work is complete.

### Who reads the spec

Assume the reader knows the codebase at a basic level only.

Start wide, then go narrow:

1. What problem does the feature solve?
2. Where does the feature sit in the system?
3. What changes, file by file?

The goal is for a human to understand how the code works. A spec is not a record of what you already know.

### What a spec contains

Use these formats first:

- Simple diagrams (ASCII or Mermaid)
- Function signatures
- Small code snippets
- Database tables and their relationships

Write prose only when the formats above cannot carry the idea. Do not write long blocks of text.

### Data lineage

For each new feature, draw the path of the data from the entry point to the end. Example:

```
POST /orders
  -> parse_order(body: dict) -> Order
  -> validate_order(order: Order) -> Order | ValidationError
  -> save_order(order: Order) -> order_id: int     [writes: orders]
  -> queue_email(order_id: int) -> None            [writes: email_jobs]
  -> 201 {"order_id": 42}
```

Name every function, its inputs, its outputs, and the tables it touches.

---

## Keeping CURRENT_STATE.md Up to Date

`spec/CURRENT_STATE.md` is a **data lineage document**. It has exactly one job: trace every user
interaction from start to finish — through each key function, showing how the data changes, how
it is stored in or retrieved from the database, and back to the frontend. Updating it is part of
completing any task, not optional.

Use the same lineage format as a spec. A spec traces one new feature; `CURRENT_STATE.md` traces
every endpoint that exists today.

**Its structure is fixed. Do not add sections to it.**

| Section | Contains |
|---|---|
| Storage map | the SQLite tables and which functions write them |
| Endpoint index | **every** route, each pointing at its lineage section |
| Numbered lineage sections | per-endpoint trace: FE call → API handler → functions → DB writes → response → what the UI does with it |
| Module reference | one line per module saying what lives there. **Light.** |
| What actually works | accurate current status |
| Known issues | accurate current status |

**Update it when:**
- An endpoint is added, removed, or changes what it reads or writes
- A function in a traced path is added, renamed, or changes behavior
- A database table or column changes
- A known bug is fixed (remove it) or found (add it)
- A spec in `spec/planned/` is completed and moved to `spec/implemented/`

**Rules:**
- **Every endpoint must appear in the endpoint index and be traced in a section.** An endpoint
  the frontend never calls still gets traced — say so, don't omit it.
- No line counts anywhere. `file.py:42` pointers are fine and useful; `(333 lines)` is not.
- Keep it factual and present-tense. Only what exists and works right now, never plans.

**Deliberately NOT in this document** — do not re-add these, they belong elsewhere:

| Not this | It lives here |
|---|---|
| Repository layout / file tree | the tree itself; people can read it |
| How to run, install, or deploy | `README.md` |
| Data models | `backend/superforecaster/models.py` |
| Environment variables | `backend/.env.example`, `backend/config.py` |
| Dependencies | `backend/pyproject.toml` |
| Test inventory | the `tests/` directory (may return later if it earns its place) |
| Why it is shaped this way | `spec/ADR.md` |

---

## Superforecasting Methodology

The agent must implement the methodology in `spec/superforecasting_methodology.md`. When writing or modifying agent prompts, cross-reference that document. The 16 principles are the spec for agent behavior — not suggestions.

---

## Code

- Python ≥ 3.12, managed with `uv`
- Pydantic AI for the agent framework
- Pydantic v2 for all data models
- Write the smallest amount of code that does the job.
- Keep the file structure flat. Add a folder only when the flat layout becomes hard to scan.
- Follow Clean Code: small functions, plain names, one job per function.
- A reader must understand the file in a few minutes.
- Do not add an abstraction for a future need. Wait until the need is real.
- No comments explaining what code does — only comments for non-obvious why.
- Write comments and docstrings in Simplified Technical English.
- Run `uv run pytest` before marking any task complete.

Check before you finish:

- [ ] Can a new engineer read the file from top to bottom and understand it?
- [ ] Did I add code that nothing calls?
- [ ] Are all the names plain words?
- [ ] Did I update `spec/CURRENT_STATE.md`?
