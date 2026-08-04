/**
 * Superforecaster — the whole client.
 *
 * One state object, one full re-render, fourteen event renderers. No build step and
 * no framework: the app is a single page whose deepest component tree is three levels,
 * and a CDN dependency would buy a diffing algorithm this does not need.
 *
 * Two things live only in this browser, matching the design: the backlog (a personal
 * queue) and saved results (a cached copy of a ForecastRecord the server already has
 * under `forecast_id`). The reasoning trail is deliberately not among them — it is not
 * persisted anywhere, and re-running the question is how you watch it again.
 */

const SAVED_KEY = "sf_saved_forecasts_v1";
const BACKLOG_KEY = "sf_backlog_v1";
const THEME_KEY = "sf_theme_v1";
const TRAIL_PREFIX = "sf_trail_v1:";
const MAX_TRAILS = 12;
const MAX_SLOTS = 5;
const POLL_MS = 4000;

const STAGE_META = {
  decompose: { num: "1", label: "Decompose", principles: "P1 · P2" },
  outside: { num: "2", label: "Find base rates", principles: "P4 · P7" },
  inside: { num: "3", label: "Adjust — inside view", principles: "P5 · P9 · P14 · P15" },
  synth: { num: "4", label: "Synthesize", principles: "P6 · P8 · P16" },
  critique: { num: "5", label: "Critique", principles: "checks.py" },
};
const STAGE_ORDER = ["decompose", "outside", "inside", "synth", "critique"];

const CATEGORIES = ["general", "finance", "economics", "politics", "ai", "energy",
                    "science", "health", "sport", "tech"];

// ---------- hyperscript ----------

/**
 * @param {string} tag  "div", or "div.cls.cls2" for classes
 * @param {object|null} attrs  `on<Event>` become listeners; everything else is an
 *   attribute, except `style` and `html` which are set as properties.
 */
function h(tag, attrs, ...children) {
  const [name, ...cls] = tag.split(".");
  const el = document.createElement(name);
  if (cls.length) el.className = cls.join(" ");

  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "class") el.className = el.className ? `${el.className} ${v}` : v;
    else if (k === "style") el.setAttribute("style", v);
    else if (k === "html") el.innerHTML = v;
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }

  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const pct = (p) => (p === null || p === undefined ? "—" : `${Math.round(p * 100)}%`);
const signed = (d) => (d > 0 ? `+${Math.round(d * 100)}` : `${Math.round(d * 100)}`);
const domainOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u || ""; } };

// ---------- state ----------

const state = {
  phase: "draft",          // draft | parsing | review | view
  theme: "light",
  draftText: "",
  fields: null,            // DraftedQuestion, editable
  critique: null,
  applied: false,
  dismissed: false,
  runs: {},                // runId -> {summary, stages[], result, lastSeq, detach}
  openId: null,
  saved: [],
  backlog: [],
  collapsed: {},           // stageKey -> true
  trailMissing: {},        // runId -> true, once hydration has been tried and failed
  expandedBacklog: {},
  toast: null,
  busy: false,
};

function loadLocal(key, fallback) {
  try { return JSON.parse(window.localStorage.getItem(key)) ?? fallback; }
  catch { return fallback; }
}

function persist(key, value) {
  try { window.localStorage.setItem(key, JSON.stringify(value)); }
  catch { /* quota or private mode — the server still has the forecast */ }
}

// ---------- the reasoning trail ----------

/**
 * Save a finished run's trail so re-opening it shows the reasoning again.
 *
 * Trails are big — a run with token-level narration runs to tens of kilobytes — so
 * each gets its own key and the oldest are evicted past MAX_TRAILS. One oversized
 * trail then costs itself rather than corrupting the saved-forecast index.
 */
function saveTrail(runId, run) {
  const payload = JSON.stringify({
    stages: run.stages,
    lastSeq: run.lastSeq,
    toolCalls: run.toolCalls,
    status: run.summary.status,
    question: run.summary.question,
    savedAt: new Date().toISOString(),
  });

  for (let attempt = 0; attempt < MAX_TRAILS; attempt++) {
    try {
      window.localStorage.setItem(TRAIL_PREFIX + runId, payload);
      pruneTrails();
      return true;
    } catch {
      // Out of quota. Drop the oldest trail and try again; give up rather than
      // clearing everything, because saved results matter more than old trails.
      if (!evictOldestTrail(runId)) return false;
    }
  }
  return false;
}

function loadTrail(runId) {
  try {
    const raw = window.localStorage.getItem(TRAIL_PREFIX + runId);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function trailKeys() {
  const keys = [];
  for (let i = 0; i < window.localStorage.length; i++) {
    const k = window.localStorage.key(i);
    if (k && k.startsWith(TRAIL_PREFIX)) keys.push(k);
  }
  return keys;
}

function trailAge(key) {
  try { return JSON.parse(window.localStorage.getItem(key)).savedAt || ""; }
  catch { return ""; }
}

function evictOldestTrail(exceptRunId) {
  const candidates = trailKeys().filter((k) => k !== TRAIL_PREFIX + exceptRunId);
  if (!candidates.length) return false;
  candidates.sort((a, b) => trailAge(a).localeCompare(trailAge(b)));
  window.localStorage.removeItem(candidates[0]);
  return true;
}

function pruneTrails() {
  const keys = trailKeys();
  if (keys.length <= MAX_TRAILS) return;
  keys.sort((a, b) => trailAge(a).localeCompare(trailAge(b)));
  keys.slice(0, keys.length - MAX_TRAILS).forEach((k) => window.localStorage.removeItem(k));
}

function dropTrail(runId) {
  try { window.localStorage.removeItem(TRAIL_PREFIX + runId); } catch { /* ignore */ }
}

const _hydrating = new Set();

/**
 * Put a run back in `state.runs` so every existing renderer works unchanged.
 *
 * Tries this browser first, then the server's own buffer — which still holds runs
 * from other tabs, and from before this tab was opened.
 *
 * Called from `renderRun`, which runs on every render, so the guard is not optional:
 * without it a run missing from both places would fire a fetch per frame forever.
 * The id stays in the set after a failure — one attempt per run per session.
 */
async function hydrateRun(runId) {
  if (state.runs[runId] || _hydrating.has(runId)) return;
  _hydrating.add(runId);

  const local = loadTrail(runId);
  if (local) {
    state.runs[runId] = {
      summary: { id: runId, question: local.question, status: local.status || "done",
                 stage: "", stage_index: 5, attempt: 1, tool_calls: local.toolCalls || 0,
                 last_seq: local.lastSeq, forecast_id: null, error: null,
                 created_at: local.savedAt, ended_at: local.savedAt },
      stages: local.stages, result: null, lastSeq: local.lastSeq,
      toolCalls: local.toolCalls || 0, detach: null, animatedUpTo: local.lastSeq,
      fromStorage: true,
    };
    scheduleRender();
    return;
  }

  try {
    const snap = await getRunSnapshot(runId);
    state.runs[runId] = { summary: snap.summary, stages: [], result: null, lastSeq: 0,
                          toolCalls: 0, detach: null, animatedUpTo: Infinity };
    snap.events.forEach((ev) => applyEvent(runId, ev));
    state.runs[runId].animatedUpTo = state.runs[runId].lastSeq;
    // The server drops runs when they age out of the registry, so take a local copy
    // while it is still there.
    saveTrail(runId, state.runs[runId]);
  } catch {
    state.trailMissing = { ...state.trailMissing, [runId]: true };
  }
  scheduleRender();
}

function setState(patch) {
  Object.assign(state, patch);
  scheduleRender();
}

/**
 * Coalesce renders into one per animation frame.
 *
 * A run emits a thought frame every 80ms and the poll ticks every 4s. Rendering
 * synchronously on each meant rebuilding the whole document a dozen times a second,
 * which is what made the page flicker and dropped focus out of the textarea mid-word.
 */
let _renderQueued = false;
function scheduleRender() {
  if (_renderQueued) return;
  _renderQueued = true;
  window.requestAnimationFrame(() => { _renderQueued = false; render(); });
}

function toast(message) {
  setState({ toast: message });
  window.clearTimeout(toast._t);
  toast._t = window.setTimeout(() => setState({ toast: null }), 3200);
}

// ---------- flow ----------

async function onReadItBack() {
  const text = state.draftText.trim();
  if (text.length < 20) return toast("Say a bit more first.");
  setState({ phase: "parsing" });
  try {
    const { parsed, critique } = await draftQuestion(text);
    setState({ phase: "review", fields: parsed, critique, applied: false, dismissed: false });
  } catch (e) {
    setState({ phase: "draft" });
    toast(e.detail || "Could not read that back.");
  }
}

function onApplyRewrite() {
  setState({
    fields: {
      ...state.fields,
      resolution_criteria: state.critique.suggested_criteria,
      resolution_source: state.critique.suggested_resolution_source || state.fields.resolution_source,
    },
    applied: true,
  });
}

/** Whether a run is worth its cost yet. Client-side only — the API accepts anything,
 *  so the CLI can still run a question nobody critiqued. */
function isResolvable() {
  return !state.critique || state.critique.is_resolvable || state.applied || state.dismissed;
}

function slotsFree() {
  const active = Object.values(state.runs).filter((r) => !isTerminal(r.summary.status));
  return Math.max(0, MAX_SLOTS - active.length);
}

const isTerminal = (s) => ["done", "error", "cancelled", "lost"].includes(s);

function runFieldsFrom(f) {
  return {
    question: f.question,
    resolution_criteria: f.resolution_criteria,
    resolution_date: f.resolution_date,
    category: f.category || "general",
    resolution_source: f.resolution_source || "",
  };
}

async function onRunNow() {
  if (!isResolvable()) return toast("Fix the criteria first — an unscoreable forecast is wasted.");
  setState({ busy: true });
  try {
    const summary = await createRun(runFieldsFrom(state.fields));
    attachRun(summary);
    setState({ phase: "view", openId: summary.id, busy: false, draftText: "", fields: null, critique: null });
  } catch (e) {
    setState({ busy: false });
    if (e.status === 429) { onQueueToBacklog(); toast("All slots busy — parked in the backlog."); }
    else toast(e.detail || "Could not start the run.");
  }
}

function onQueueToBacklog() {
  const item = { id: `bk_${Math.random().toString(16).slice(2, 8)}`, ...runFieldsFrom(state.fields) };
  const backlog = [item, ...state.backlog];
  persist(BACKLOG_KEY, backlog);
  setState({ backlog, phase: "draft", draftText: "", fields: null, critique: null });
  window.scrollTo(0, 0);
}

/**
 * Restart a failed run from its last completed node.
 *
 * The budget is raised by default because the usual reason to be here is that it ran
 * out — resuming with the same one walks into the same wall.
 */
async function onResume(runId) {
  const run = state.runs[runId];
  const current = run?.summary?.max_iterations || 5;
  const depth = window.prompt(
    "Search depth to resume with (higher = more tool calls per research step):",
    String(Math.min(20, current * 2)),
  );
  if (depth === null) return;

  try {
    const summary = await resumeRun(runId, Number(depth) || undefined);
    if (run) {
      run.summary = { ...run.summary, ...summary };
      run.detach = null;
    }
    // Re-attach from where the frames stopped: the resumed run continues the same
    // sequence, so replaying from the start would duplicate the whole trail.
    attachRun(summary, (run?.lastSeq ?? 0) + 1);
    setState({ phase: "view", openId: runId });
  } catch (e) {
    toast(e.detail || "Could not resume.");
  }
}

async function onRunFromBacklog(id) {
  const item = state.backlog.find((b) => b.id === id);
  if (!item) return;
  try {
    const summary = await createRun(runFieldsFrom(item));
    const backlog = state.backlog.filter((b) => b.id !== id);
    persist(BACKLOG_KEY, backlog);
    attachRun(summary);
    setState({ backlog, phase: "view", openId: summary.id });
  } catch (e) {
    toast(e.status === 429 ? "Still no free slots." : e.detail || "Could not start it.");
  }
}

function onDropBacklog(id) {
  const backlog = state.backlog.filter((b) => b.id !== id);
  persist(BACKLOG_KEY, backlog);
  setState({ backlog });
}

/**
 * Remove a finished run from this browser.
 *
 * Local only — the forecast itself stays on the server under its `forecast_id`, and
 * the run row stays in the `runs` table. This clears the card, not the record.
 */
function onDeleteSaved(id) {
  const saved = state.saved.filter((r) => r.id !== id);
  persist(SAVED_KEY, saved);
  dropTrail(id);
  delete state.runs[id];
  setState({ saved, openId: state.openId === id ? null : state.openId,
             phase: state.openId === id ? "draft" : state.phase });
}

// ---------- streaming ----------

/** Register a run and open its stream. Idempotent — reopening is a no-op. */
function attachRun(summary, fromSeq = 0) {
  const existing = state.runs[summary.id];
  if (existing && existing.detach) return existing;

  const local = {
    summary,
    stages: [],
    result: null,
    lastSeq: fromSeq,
    toolCalls: 0,
    detach: null,
  };
  state.runs[summary.id] = local;

  local.detach = openRunStream(
    summary.id,
    (ev) => { applyEvent(summary.id, ev); scheduleRender(); },
    (reason) => {
      const r = state.runs[summary.id];
      if (r) {
        r.detach = null;
        if (!isTerminal(r.summary.status)) r.summary = { ...r.summary, status: reason === "disconnected" ? "lost" : r.summary.status };
      }
      scheduleRender();
    },
    fromSeq,
  );
  return local;
}

/**
 * Fold one event into the local run.
 *
 * A stage group is keyed by `stage + attempt`, which is what makes the Synthesize
 * retry render as its own "attempt 2" card instead of overwriting attempt 1.
 */
function applyEvent(runId, ev) {
  const run = state.runs[runId];
  if (!run) return;
  run.lastSeq = Math.max(run.lastSeq, ev.seq);

  switch (ev.type) {
    case "stage": {
      const key = `${ev.payload.stage}-${ev.payload.attempt}`;
      if (!run.stages.some((s) => s.key === key)) {
        run.stages.push({ key, stage: ev.payload.stage, attempt: ev.payload.attempt, items: [] });
      }
      run.summary = { ...run.summary, stage: ev.payload.stage, attempt: ev.payload.attempt,
                      stage_index: STAGE_ORDER.indexOf(ev.payload.stage) + 1 };
      return;
    }
    case "result":
      run.result = ev.payload;
      run.summary = { ...run.summary, forecast_id: ev.payload.forecast_id };
      saveResult(runId, ev.payload);
      return;
    case "end":
      run.summary = { ...run.summary, status: ev.payload.status, forecast_id: ev.payload.forecast_id };
      // Persist the trail now that it is complete. The server drops it when the run
      // ages out of the registry, so this browser is where it lives from here on.
      saveTrail(runId, run);
      return;
  }

  const group = run.stages[run.stages.length - 1];
  if (!group) return;

  if (ev.type === "query") run.toolCalls += 1;

  // Consecutive thought deltas belong to one paragraph — the server coalesces on an
  // 80ms timer, which still leaves several frames inside a single stretch of prose.
  const last = group.items[group.items.length - 1];
  if (ev.type === "thought" && last && last.type === "thought") {
    last.payload = { ...last.payload, delta: last.payload.delta + ev.payload.delta };
    return;
  }
  group.items.push(ev);
}

function saveResult(runId, payload) {
  const record = {
    id: runId,
    forecast_id: payload.forecast_id,
    question: payload.question,
    probability: payload.probability,
    anchor: payload.anchor,
    confidence: payload.confidence,
    reasoning: payload.reasoning,
    waterfall: payload.waterfall,
    violations: payload.violations,
    stamp: new Date().toISOString(),
  };
  const saved = [record, ...state.saved.filter((r) => r.id !== runId)];
  persist(SAVED_KEY, saved);
  state.saved = saved;
}

/**
 * Reconcile with the server — catches runs started in another tab, and runs the
 * server lost to a restart while this tab was asleep.
 *
 * An attached stream is the authority for its own run. Poll results are several
 * seconds ahead of the frames still being delivered, so copying them over would let
 * the rail claim "Synthesize 4/5" while the trail is still rendering the base rates,
 * or flip a run to `done` with events left to draw.
 */
async function pollRuns() {
  try {
    let changed = false;
    for (const summary of await listRuns()) {
      const local = state.runs[summary.id];
      if (!local) {
        if (!isTerminal(summary.status)) { attachRun(summary); changed = true; }
      } else if (!local.detach) {
        // No live stream: either never opened, or dropped. Reopen from where the
        // frames stopped rather than replaying the whole timeline.
        if (!isTerminal(summary.status)) { attachRun(summary, local.lastSeq + 1); changed = true; }
        else if (local.summary.status !== summary.status) {
          local.summary = { ...local.summary, ...summary };
          changed = true;
        }
      }
    }
    // Only re-render when the poll actually learned something. An unconditional
    // render here rebuilt the page every four seconds forever, which is what made an
    // idle page flicker and blew away whatever was focused.
    if (changed) scheduleRender();
  } catch { /* offline; the next tick tries again */ }
}

// ---------- event renderers ----------

const EVENT_RENDERERS = {
  thought: (p) =>
    h("div.ev.thought", {}, p.delta, h("span.caret", {}, "▍")),

  note: (p) =>
    h("div.ev.note", {},
      h("div.micro", {}, p.label),
      h("div.dim", {}, p.text)),

  query: (p) =>
    h("div.ev.query", {},
      h("span.chip", {}, p.tool),
      h("span.dim", {}, `“${p.q}”`)),

  source: (p) =>
    h("div.ev.source", {},
      h("span", { style: `width:6px;height:6px;border-radius:50%;background:var(--pv-text-3);flex:none;margin-top:6px` }),
      h("div", {},
        h("div", {}, p.title || domainOf(p.url)),
        h("div.micro", {}, domainOf(p.url), p.published_date ? ` · ${p.published_date.slice(0, 10)}` : ""),
        p.snippet ? h("div.dim.mono", {}, p.snippet) : null)),

  sub: (p) =>
    h("div.ev.sub", {},
      h("div.evhead", {},
        h("span.num-strong", {}, pct(p.p)),
        h("span.chip", { class: p.knowability === "researchable" ? "for" : "" }, p.knowability),
        h("span.micro", {}, p.confidence)),
      h("div", {}, p.question),
      h("div.dim", {}, p.rationale)),

  ref: (p) =>
    h("div.ev.ref", {},
      h("div.evhead", {},
        h("span.num-strong", {}, pct(p.rate)),
        h("span.micro", {}, `n=${p.n}`)),
      h("div", {}, p.name),
      h("div.minibar", {}, h("i", { style: `width:${Math.min(100, p.rate * 100)}%` })),
      h("div.micro", {}, p.source)),

  analog: (p) =>
    h("div.ev.analog", {},
      h("div.evhead", {},
        h("span.chip", { class: p.outcome >= 1 ? "for" : "against" }, p.outcome >= 1 ? "yes" : "no"),
        h("span", {}, p.description)),
      h("div.dim", {}, p.relevance)),

  adj: (p) =>
    h("div.ev.adj", { class: p.noise ? "noise" : "" },
      h("div.evhead", {},
        h("span.num-strong", {
          style: `color:${p.noise ? "var(--pv-text-3)" : p.dir === "up" ? "var(--pv-green)" : "var(--pv-red)"}`,
        }, p.noise ? "0 pts" : `${p.dir === "up" ? "+" : "−"}${Math.round(p.mag * 100)} pts`),
        p.noise ? h("span.chip", {}, "noise") : null),
      h("div", {}, p.evidence),
      h("div.dim", {}, h("span.micro", {}, "flip test "), p.flip)),

  bias: (p) =>
    h("div.ev.bias", {},
      h("div.micro", {}, p.bias),
      h("div.dim", {}, p.assessment)),

  check: (p) =>
    h("details.ev.check", {
      open: !p.ok || undefined,
      style: p.ok ? "" : "background:var(--pv-red-fill);border-color:var(--pv-red-soft)",
    },
      h("summary", {},
        h("span", { style: `color:${p.ok ? "var(--pv-green)" : "var(--pv-red)"}` }, p.ok ? "✓" : "✗"),
        h("span.k", {}, p.check),
        h("span.micro.showwork", {}, "show the numbers")),
      // Passing checks carry no detail — the validators only produce a message when
      // something failed, and inventing one would be the UI making things up.
      p.detail ? h("div.dim", { style: "margin-top:5px" }, p.detail) : null,
      renderCheckEvidence(p.name, p.evidence || {})),

  brief: (p) =>
    h("div.ev.brief", {},
      h("div.micro", {}, "What attempt 2 is told"),
      h("p.dim", { style: "margin:5px 0" },
        `The decomposition, base rate, and adjustments are unchanged — only the `
        + `instruction changes. Base rate ${pct(p.anchor)} plus the signed non-noise `
        + `adjustments implies ${pct(p.implied)}.`),
      h("pre.prompt", {}, p.correction || p.arithmetic)),

  draft: (p) =>
    h("div.ev.draft", {},
      h("div.evhead", {}, h("span.num-strong", {}, pct(p.p)), h("span.micro", {}, p.note))),

  route: (p) => h("div.ev.route", {}, "↩ ", p.text),

  error: (p) =>
    h("div.ev.route", {},
      h("div", {}, p.message),
      p.hint ? h("div", { style: "margin-top:5px" }, p.hint) : null,
      p.resumable
        ? h("div.micro", { style: "margin-top:6px" },
            `${(p.completed_stages || []).length} stage`
            + `${(p.completed_stages || []).length === 1 ? "" : "s"} already complete`
            + `${(p.completed_stages || []).length ? " — " + (p.completed_stages || [])
                .map((s) => STAGE_META[s]?.label || s).join(", ") : ""}`
            + ". Resuming re-runs only the step that failed.")
        : null),

  resume: (p) =>
    h("div.ev.brief", {},
      h("div.micro", {}, "Resumed"),
      h("div.dim", {},
        `Picking up at ${STAGE_META[p.from_node]?.label || p.from_node}. `
        + `Search depth ${p.max_iterations}. `
        + `${(p.completed_stages || []).length} earlier stage`
        + `${(p.completed_stages || []).length === 1 ? "" : "s"} kept.`)),

  truncated: (p) =>
    h("div.ev.truncated", {}, `${p.count} earlier events were dropped from the buffer.`),
};

/**
 * The material a check reached its verdict on.
 *
 * One renderer per check because the interesting numbers differ: P6 is an arithmetic
 * walk, P7 is a spread against a threshold, P15 is five slots and which were filled.
 * A generic key/value dump would technically show the same data and tell you nothing.
 */
function renderCheckEvidence(name, e) {
  const row = (label, value, tone) =>
    h("div.evrow", {},
      h("span.lbl", {}, label),
      h("span", { style: tone ? `color:${tone}` : "" }, value));

  switch (name) {
    case "derivation":
      return h("div.work", {},
        row("anchor", pct(e.anchor)),
        (e.walk || []).map((w) =>
          row(w.evidence, w.is_noise ? "noise · 0" : `${signed(w.delta)} → ${pct(w.running)}`,
              w.is_noise ? "var(--pv-text-3)" : w.delta > 0 ? "var(--pv-green)" : "var(--pv-red)")),
        row("implied", pct(e.implied)),
        row("stated", pct(e.stated)),
        row("drift vs slack", `${(e.drift ?? 0).toFixed(3)} vs ${(e.slack ?? 0).toFixed(3)}`,
            e.drift > e.slack ? "var(--pv-red)" : "var(--pv-green)"));

    case "dragonfly":
      return h("div.work", {},
        (e.classes || []).map((c) => row(c.name, `${pct(c.base_rate)} · n=${c.sample_size}`)),
        row("spread vs threshold", `${(e.spread ?? 0).toFixed(3)} vs ${(e.threshold ?? 0).toFixed(3)}`,
            e.spread > e.threshold ? "var(--pv-red)" : "var(--pv-green)"),
        row("disagreement stated", e.disagreement ? "yes" : "no",
            e.disagreement ? "var(--pv-green)" : "var(--pv-text-3)"));

    case "bias_coverage":
      return h("div.work", {},
        (e.assessed || []).map((b) => row(b.bias, b.assessment ? "assessed" : "empty",
                                          b.assessment ? "var(--pv-green)" : "var(--pv-red)")),
        (e.missing || []).map((b) => row(b, "never addressed", "var(--pv-red)")));

    case "signal_vs_noise":
      return h("div.work", {},
        (e.adjustments || []).map((a) =>
          row(a.evidence, a.flip_test ? (a.is_noise ? "flip test · noise · 0" : "flip test present")
                                      : "no flip test",
              a.flip_test ? "var(--pv-green)" : "var(--pv-red)")));

    case "disconfirming":
      return h("div.work", {},
        row("steel_man", e.steel_man ? "present" : "empty",
            e.steel_man ? "var(--pv-green)" : "var(--pv-red)"),
        row("what_would_change_my_mind", e.what_would_change_my_mind ? "present" : "empty",
            e.what_would_change_my_mind ? "var(--pv-green)" : "var(--pv-red)"),
        row("adjustment directions", (e.directions || []).join(", ") || "none",
            new Set(e.directions || []).size <= 1 && (e.real_adjustments || 0) >= 2
              ? "var(--pv-red)" : ""));

    case "decomposition":
      return h("div.work", {},
        (e.sub_claims || []).map((s) =>
          row(s.question, `${pct(s.probability)} · ${s.knowability}`
              + (s.has_rationale ? "" : " · NO RATIONALE"),
              s.has_rationale ? "" : "var(--pv-red)")),
        row("researchable", `${e.researchable} of ${(e.sub_claims || []).length}`,
            e.researchable ? "var(--pv-green)" : "var(--pv-red)"),
        row("chain_note", e.chain_note || "empty", e.chain_note ? "" : "var(--pv-red)"));

    case "calibration_hygiene":
      return h("div.work", {},
        row("probability", pct(e.probability)),
        row("allowed band", `${pct(e.floor)} – ${pct(e.ceiling)}`),
        row("confidence", e.confidence),
        row("class spread vs agreement", `${(e.spread ?? 0).toFixed(3)} vs ${(e.agreement_threshold ?? 0).toFixed(3)}`));

    default:
      return null;
  }
}

// ---------- render ----------

function render() {
  const root = document.getElementById("root");
  document.documentElement.setAttribute("data-theme", state.theme);

  const focus = captureFocus();
  // `replaceChildren` is raw DOM and does NOT skip nulls the way `h` does — it
  // stringifies them, which is how a literal "null" ended up on the page.
  root.replaceChildren(
    ...[
      renderHeader(),
      h("div.shell", {}, renderRail(), renderMain()),
      state.toast ? h("div.toast", {}, state.toast) : null,
    ].filter(Boolean),
  );
  restoreFocus(focus);
}

/**
 * Remember which field the caret was in, and where.
 *
 * A full re-render destroys the focused element. Rendering is coalesced now, so this
 * fires far less often — but a run streaming in the background still re-renders while
 * someone is typing in the draft box, and losing the caret mid-sentence is worse than
 * anything the re-render was for. Fields are matched by `data-focus` rather than by
 * position, so a changing tree cannot move focus to the wrong input.
 */
function captureFocus() {
  const el = document.activeElement;
  const key = el && el.getAttribute && el.getAttribute("data-focus");
  if (!key) return null;
  return { key, start: el.selectionStart, end: el.selectionEnd, scroll: el.scrollTop };
}

function restoreFocus(saved) {
  if (!saved) return;
  const el = document.querySelector(`[data-focus="${saved.key}"]`);
  if (!el) return;
  el.focus({ preventScroll: true });
  if (saved.start !== null && saved.start !== undefined && el.setSelectionRange) {
    try { el.setSelectionRange(saved.start, saved.end); } catch { /* not a text field */ }
  }
  el.scrollTop = saved.scroll;
}

function renderHeader() {
  return h("header.hdr", {},
    h("div.mark", {}, "S"),
    h("div.wordmark", {}, "Superforecaster"),
    h("div.spacer", {}),
    h("div.micro", {}, `${MAX_SLOTS - slotsFree()} of ${MAX_SLOTS} slots running`),
    h("button.btn.tiny.ghost", {
      onClick: () => {
        const token = window.prompt("Admin token (needed to start a run):", getAdminToken() || "");
        if (token !== null) { setAdminToken(token.trim() || null); toast(token.trim() ? "Token saved." : "Token cleared."); }
      },
    }, getAdminToken() ? "Admin ✓" : "Admin"),
    h("button.btn.tiny", {
      onClick: () => {
        const theme = state.theme === "dark" ? "light" : "dark";
        persist(THEME_KEY, theme);
        setState({ theme });
      },
    }, "Theme"),
  );
}

function renderRail() {
  const active = Object.values(state.runs).filter((r) => !isTerminal(r.summary.status));

  return h("aside.rail", {},
    h("button.btn.tiny.ghost", { onClick: () => setState({ phase: "draft", openId: null }) }, "← Home"),

    h("div.micro", { style: "margin:16px 0 8px" }, `Running · ${active.length} of ${MAX_SLOTS}`),
    active.length === 0 ? h("div.empty", {}, "Nothing running.") : null,
    active.map((r) =>
      h("div.rowcard", { class: state.openId === r.summary.id ? "on" : "",
                         onClick: () => setState({ phase: "view", openId: r.summary.id }) },
        h("div.evhead", {},
          h("span.micro", {}, STAGE_META[r.summary.stage]?.label || "starting"),
          h("span.micro", {}, `${r.summary.stage_index}/5`)),
        h("div", {}, r.summary.question),
        h("div.bar", {}, h("i", { style: `width:${(r.summary.stage_index / 5) * 100}%` })))),

    h("div.micro", { style: "margin:20px 0 8px" }, `Saved · ${state.saved.length}`),
    state.saved.length === 0
      ? h("div.empty", {}, "Nothing here yet. Finished runs save themselves to this browser.")
      : null,
    state.saved.map((r) =>
      h("div.rowcard", { class: state.openId === r.id ? "on" : "",
                         onClick: () => setState({ phase: "view", openId: r.id }) },
        h("div.evhead", {},
          h("span.num-strong", {}, pct(r.probability)),
          h("span.chip", { class: r.probability < 0.5 ? "against" : "for" }, r.probability < 0.5 ? "no" : "yes"),
          h("span.micro", {}, r.confidence)),
        h("div", {}, r.question),
        h("div.micro", {}, r.stamp.slice(0, 10)),
        h("button.del", {
          title: "Remove from this browser — the forecast stays on the server",
          onClick: (e) => { e.stopPropagation(); onDeleteSaved(r.id); },
        }, "×"))),

    h("div.micro", { style: "margin-top:22px" }, "Autosaved to this browser"),
  );
}

function renderMain() {
  if (state.phase === "view") return h("main.main", {}, renderRun());
  return h("main.main", {},
    state.phase === "draft" ? renderDraft() : null,
    state.phase === "parsing" ? renderParsing() : null,
    state.phase === "review" ? renderReview() : null,
    renderBacklog(),
  );
}

function renderDraft() {
  return h("section.panel", {},
    h("h1", {}, "New forecast"),
    h("p.dim", {}, "Describe what you want forecast."),
    h("p.dim", {}, "Write it the way you think about it — the question, what counts as YES, " +
      "when it settles, and who you would trust to settle it. The criteria critic reads it " +
      "back and names what two reasonable people could still argue over on resolution day."),
    h("textarea.ta", {
      "data-focus": "draft",
      style: "min-height:150px",
      placeholder: "Will Anthropic go public before the end of 2026? Resolves YES if…",
      value: state.draftText,
      onInput: (e) => { state.draftText = e.target.value; },
    }),
    h("div", { style: "display:flex;gap:8px;align-items:center;margin-top:12px" },
      h("span.chip", {}, "P3 · criteria critic"),
      h("span.micro", {}, "One pass, no chat."),
      h("div.spacer", {}),
      h("button.btn.primary", { onClick: onReadItBack }, "Read it back")),
  );
}

function renderParsing() {
  return h("section.panel", {},
    h("div", { style: "display:flex;gap:10px;align-items:center" },
      h("span.spin", {}), h("span.pulse", {}, "Reading criteria for resolvability")),
  );
}

function renderReview() {
  const f = state.fields;
  const c = state.critique;
  const ok = isResolvable();
  const field = (label, key, opts = {}) =>
    h("label.field", {},
      h("span.micro", {}, label),
      opts.textarea
        ? h("textarea.ta", { "data-focus": key, class: opts.bad ? "bad" : "", value: f[key] || "",
                             onInput: (e) => { f[key] = e.target.value; } })
        : opts.select
        ? h("select.inp", { "data-focus": key, onChange: (e) => { f[key] = e.target.value; } },
            CATEGORIES.map((cat) => h("option", { value: cat, selected: (f[key] || "general") === cat }, cat)))
        : h("input.inp", { "data-focus": key, class: opts.bad ? "bad" : "", type: opts.type || "text",
                           value: opts.type === "date" ? (f[key] || "").slice(0, 10) : (f[key] || ""),
                           onInput: (e) => { f[key] = e.target.value; } }));

  return h("div", {},
    h("section.panel", {},
      h("div.evhead", {}, h("h2", {}, "Parsed from your text"), h("div.spacer", {}),
        h("button.btn.tiny", { onClick: () => setState({ phase: "draft" }) }, "Rewrite")),
      field("Question", "question", { textarea: true }),
      field("Resolution criteria", "resolution_criteria", { textarea: true, bad: !ok }),
      h("div.grid2", {},
        field("Resolution date", "resolution_date", { type: "date" }),
        field("Category", "category", { select: true })),
      field("Resolution source", "resolution_source", { bad: !f.resolution_source }),

      h("div", { style: "display:flex;gap:8px;align-items:center;margin-top:6px" },
        h("span.micro", { style: "flex:1" },
          !ok ? "Blocked: the criteria have to be adjudicable before a run is worth its cost."
              : slotsFree() <= 0 ? "All five run slots are busy. Add it to the backlog."
              : "Full graph, two clamped search tools. Expect five to eight minutes."),
        h("button.btn", { onClick: onQueueToBacklog }, "Add to backlog"),
        h("button.btn.primary", { disabled: !ok || slotsFree() <= 0 || state.busy, onClick: onRunNow },
          state.busy ? "Starting…" : "Run now"))),

    c ? h("section.panel", {},
      h("div.evhead", {},
        h("span.micro", {}, "Resolvability"),
        h("span.chip", { class: ok ? "for" : "against" }, ok ? "adjudicable" : "not resolvable")),
      h("p.dim", {}, ok
        ? "Two people reading these criteria on resolution day would reach the same verdict. Cleared to run."
        : `${c.ambiguities.length} ambiguities and ${c.missing.length} structural gaps. Two people reading this on resolution day could argue, so the forecast would not be scoreable.`),

      c.ambiguities.length ? h("div", {},
        h("div.micro", { style: "margin-top:12px" }, `Ambiguities · ${c.ambiguities.length}`),
        c.ambiguities.map((t, i) => h("div.ev.note", { style: "margin-top:6px" },
          h("span.micro", {}, String(i + 1)), " ", t))) : null,

      c.missing.length ? h("div", {},
        h("div.micro", { style: "margin-top:12px" }, `Missing · ${c.missing.length}`),
        c.missing.map((t) => h("div.ev.note", { style: "margin-top:6px" }, "— ", t))) : null,

      !ok ? h("div", { style: "margin-top:16px" },
        h("div.micro", {}, "Suggested rewrite"),
        h("p", { style: "margin-top:6px" }, c.suggested_criteria),
        h("div.micro", {}, `Source · ${c.suggested_resolution_source}`),
        h("div", { style: "display:flex;gap:8px;margin-top:10px" },
          h("button.btn.primary", { onClick: onApplyRewrite }, "Apply rewrite"),
          h("button.btn", { onClick: () => setState({ dismissed: true }) }, "Keep mine"))) : null,
    ) : null,
  );
}

function renderBacklog() {
  return h("section.panel", {},
    h("div.evhead", {},
      h("h2", {}, `Backlog · ${state.backlog.length}`),
      h("div.spacer", {}),
      h("span.micro", {}, slotsFree() > 0 ? `${slotsFree()} of ${MAX_SLOTS} run slots free` : "All run slots busy — these wait")),
    state.backlog.length === 0
      ? h("div.empty", {}, "Backlog is empty. Anything you add above waits here until you have a slot free.")
      : null,
    state.backlog.map((b) =>
      h("div.card", { style: "margin-bottom:8px" },
        h("div.evhead", {},
          h("span.chip", {}, "queued"),
          h("span.chip", {}, b.category),
          h("span.micro", {}, `resolves ${(b.resolution_date || "").slice(0, 10)}`)),
        h("div", {}, b.question),
        h("div", { style: "display:flex;gap:8px;margin-top:9px" },
          h("button.btn.tiny.primary", { disabled: slotsFree() <= 0, onClick: () => onRunFromBacklog(b.id) }, "Run"),
          h("button.btn.tiny.ghost", { onClick: () => onDropBacklog(b.id) }, "Remove")))),
  );
}

function renderRun() {
  const run = state.runs[state.openId];
  const saved = state.saved.find((r) => r.id === state.openId);
  const result = run?.result || saved;
  const live = run && !isTerminal(run.summary.status);

  // Opening a finished run from a previous session: pull its trail back from storage
  // (or the server's buffer) rather than telling the user it is gone.
  if (!run && state.openId) hydrateRun(state.openId);

  const gone = state.trailMissing[state.openId];
  if (!run && !saved) {
    return h("section.panel", {}, h("p.dim", {},
      gone ? "That run is gone from this browser and from the server."
           : "Loading…"));
  }

  return h("div", {},
    h("section.panel", {},
      h("div.evhead", {},
        h("span.chip", { class: live ? "warn" : "for" }, live ? "streaming" : (run?.summary.status || "complete")),
        h("span.mono", {}, state.openId),
        run ? h("span.micro", {}, `${run.toolCalls} tool calls`) : null,
        h("div.spacer", {}),
        live ? h("button.btn.tiny", { onClick: () => cancelRun(state.openId).catch(() => {}) }, "Cancel") : null,
        run && run.summary.status === "error"
          ? h("button.btn.tiny.primary", { onClick: () => onResume(state.openId) }, "Resume")
          : null),
      h("h1", { style: "margin-top:10px" }, run?.summary.question || saved?.question),
      live ? h("p.micro", {}, "Running server-side. Safe to close this tab — results autosave when the run lands.") : null,
      run?.summary.status === "lost"
        ? h("p.dim", {}, "The server restarted while this was running. The trail is gone; re-run the question to watch it again.")
        : null,
    ),

    result ? renderResultCard(result) : null,
    run && run.stages.length
      ? renderTrail(run)
      : h("section.panel", {}, h("p.dim", {},
          run || gone
            ? "No stored trail for this run — it finished before trails were kept, or "
              + "storage was cleared. Re-run the question to watch it again."
            : "Loading the reasoning trail…")),
  );
}

function renderResultCard(r) {
  const delta = r.anchor === null || r.anchor === undefined ? null : r.probability - r.anchor;
  return h("section.panel", {},
    h("div", { style: "display:flex;gap:34px;flex-wrap:wrap;align-items:flex-start" },
      h("div", {},
        h("div.micro", {}, "Probability of resolution"),
        h("div.bignum", {}, `${Math.round(r.probability * 100)}%`)),
      h("div.stat", {},
        h("span.micro", {}, "Lean"),
        h("span.chip", { class: r.probability < 0.5 ? "against" : "for" },
          r.probability < 0.5 ? "resolves NO" : "resolves YES")),
      delta !== null ? h("div.stat", {},
        h("span.micro", {}, "Movement"),
        h("span", { style: `color:${delta < 0 ? "var(--pv-red)" : delta > 0 ? "var(--pv-green)" : "var(--pv-text-2)"}` },
          `${delta < 0 ? "↓" : delta > 0 ? "↑" : "→"} ${Math.abs(Math.round(delta * 100))} pts`),
        h("span.micro", {}, `vs ${pct(r.anchor)} anchor`)) : null,
      h("div.stat", {}, h("span.micro", {}, "Confidence"), h("span", {}, r.confidence)),
    ),

    r.violations && r.violations.length
      ? h("div.ev.route", { style: "margin-top:14px" },
          `${r.violations.length} check${r.violations.length === 1 ? "" : "s"} still failing after the retry: ` +
          r.violations.map((v) => v.name).join(", "))
      : null,

    r.reasoning ? h("div", { style: "margin-top:18px" },
      h("div.micro", {}, "Reasoning"),
      r.reasoning.split(/\n\n+/).map((para) => h("p", {}, para))) : null,

    r.waterfall && r.waterfall.length ? renderWaterfall(r.waterfall) : null,
  );
}

function renderWaterfall(rows) {
  const scale = Math.max(...rows.map((w) => w.running)) * 1.3 || 1;
  return h("div", { style: "margin-top:18px" },
    h("div.micro", {}, "Anchor → adjustments → stated"),
    h("div.wf", {}, rows.map((w) =>
      h("div.wfrow", { class: w.kind },
        h("div.lbl", { title: w.label }, w.label),
        h("div", { style: `text-align:right;color:${w.kind === "up" ? "var(--pv-green)" : w.kind === "down" ? "var(--pv-red)" : "var(--pv-text-3)"}` },
          w.delta === null || w.delta === undefined || w.delta === 0 ? "" : signed(w.delta)),
        h("div.track", {}, h("i", { style: `width:${(w.running / scale) * 100}%` })),
        h("div.num-strong", { style: "text-align:right" }, pct(w.running))))));
}

function renderTrail(run) {
  // Only events that have never been drawn get the entrance animation. Without this
  // every re-render replays the fade-in on the whole trail at once, which is what the
  // flicker actually was — not a repaint, sixty elements animating in unison.
  const animatedUpTo = run.animatedUpTo ?? -1;

  const section = h("section", {},
    run.stages.map((group, i) => {
      const meta = STAGE_META[group.stage] || { num: "?", label: group.stage, principles: "" };
      const busy = !isTerminal(run.summary.status) && i === run.stages.length - 1;
      const collapsed = state.collapsed[group.key];
      return h("div.stage", { class: busy ? "busy" : "" },
        h("header", { onClick: () => { state.collapsed[group.key] = !collapsed; scheduleRender(); } },
          h("div.num", {}, meta.num),
          h("div", {}, group.attempt > 1 ? `${meta.label} · attempt ${group.attempt}` : meta.label),
          h("div.micro", {}, meta.principles),
          h("div.spacer", {}),
          busy ? h("span.micro.pulse", {}, "working") : h("span.micro", {}, `${group.items.length}`)),
        collapsed ? null : h("div.items", {},
          group.items.map((ev) => {
            const renderer = EVENT_RENDERERS[ev.type];
            if (!renderer) return null;
            const node = renderer(ev.payload);
            if (ev.seq > animatedUpTo) node.classList.add("fresh");
            return node;
          })));
    }),
  );

  run.animatedUpTo = run.lastSeq;
  return section;
}

// ---------- boot ----------

state.saved = loadLocal(SAVED_KEY, []);
state.backlog = loadLocal(BACKLOG_KEY, []);
state.theme = loadLocal(THEME_KEY, window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light");

render();
pollRuns();
window.setInterval(pollRuns, POLL_MS);
