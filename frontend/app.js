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
/** Mirrors `models.MAX_SEARCH_DEPTH`. Clamp here so a raise never bounces as a 422. */
const MAX_SEARCH_DEPTH = 50;

const STAGE_META = {
  decompose: { num: "1", label: "Decompose", principles: [1, 2] },
  lenses: { num: "2", label: "Choose lenses", principles: [4, 7] },
  outside: { num: "3", label: "Find base rates", principles: [4] },
  inside: { num: "4", label: "Adjust — inside view", principles: [5, 9] },
  reflect: { num: "5", label: "Reflect", principles: [14, 15] },
  synth: { num: "6", label: "Synthesize", principles: [6, 8, 16] },
  critique: { num: "7", label: "Critique", principles: [] },
  // Not a graph step — a seam in the trail, so the stages above it are visibly the
  // ones that already ran rather than looking like part of this attempt.
  resume: { num: "↻", label: "Resumed", principles: [] },
};
const STAGE_ORDER = ["decompose", "lenses", "outside", "inside", "reflect", "synth", "critique"];

/**
 * One line per methodology principle, so a bare "P7" explains itself.
 *
 * Keyed by check *slot name*, not principle number: `check_decomposition` occupies the
 * P1·P2 slot but reports principle 2 when its knowability arm fails, so keying off the
 * number would show the wrong blurb under the right label.
 *
 * `spec/superforecasting_methodology.md` is the source of truth. These are condensed
 * from it; if the two disagree, the doc is right.
 */
const PRINCIPLES = {
  decomposition: "P1 · P2 — Break the question into 3–5 sub-claims you could argue about separately, and say which ones have a lookupable base rate and which are judgment.",
  dragonfly: "P7 — Consult several reference classes, not one. When they disagree, that disagreement is information about how uncertain the question is, and has to be explained rather than averaged away.",
  aggregation: "P7 — The single anchor has to be the weighted average its own reference classes imply. A blend nobody can recompute is a number you cannot check.",
  citations: "P4 — Base rates get looked up, not reasoned to. Every cited URL must be one the agent actually retrieved.",
  signal_vs_noise: "P9 — For each piece of evidence, ask what your estimate would do if you had found the opposite. If the answer is 'nothing much', it is noise and must move the number by zero.",
  disconfirming: "P14 — Look for what would prove you wrong before you settle, not after. Argue the opposing case properly, and name what would change your mind.",
  bias_coverage: "P15 — Address all five named biases explicitly: confirmation, availability, narrative, scope insensitivity, and anchoring.",
  derivation: "P6 — The final probability must equal the base rate plus the stated adjustments. This is what stops a compelling story pulling the estimate away from the evidence.",
  calibration_hygiene: "P16 — A well-calibrated 60% beats a miscalibrated 90%. An extreme probability is allowed, but it has to be argued for rather than asserted.",
};

// ---------- derivations ----------
//
// The backend streams the typed objects its agents returned and nothing else — a whole
// `Decomposition`, a whole `OutsideView`. Everything below turns those into what the
// screen needs. It used to live in `runs.py` as ~340 lines of projection, which put
// layout decisions in Python and six hundred lines from the CSS they served.
//
// Each of these mirrors a function in `checks.py`, deliberately: the check and the
// picture have to agree about what the evidence implies, so they compute it the same way.

/** The populations a sub-question was viewed through. */
function lensesFor(id, outside) {
  if (!outside || !id) return [];
  return outside.lenses.filter((l) => (l.sub_claim_ids || []).includes(id));
}

/** A lens's base rate: pooled hits over pooled n. Derived, never asserted. */
function lensRate(lens) {
  const n = (lens.evidence || []).reduce((t, e) => t + e.n, 0);
  if (!n) return 0;
  return (lens.evidence || []).reduce((t, e) => t + e.hits, 0) / n;
}

/** How many cases a lens rests on, and how they were gathered. */
function lensEvidenceSummary(lens) {
  return (lens.evidence || []).map((e) => `${e.hits} of ${e.n} ${e.kind}`).join(" · ");
}

/** A lens's rate after its own modifiers. Only adjustments naming it apply. */
function adjustedLensRate(lens, inside) {
  const moved = adjustmentsForLens(lens, inside).reduce((n, a) => n + signedAdjustment(a), 0);
  return Math.min(1, Math.max(0, lensRate(lens) + moved));
}

/** The modifiers that move one lens. */
function adjustmentsForLens(lens, inside) {
  if (!inside) return [];
  return inside.adjustments.filter((a) => a.lens_name === lens.name);
}

/**
 * A sub-question's rate: its adjusted lenses blended by relevance.
 *
 * `n` is deliberately absent. A lens measured over 12 cases outweighs one measured over
 * 230 when it fits better — sample size says how well a population was *measured*, not
 * how much it *resembles this case*, and only the second is what a reference class is
 * for. Mirrors `checks.sub_claim_rate`; if the two ever disagree the picture is lying.
 */
function subClaimRate(id, outside, inside) {
  const lenses = lensesFor(id, outside);
  const total = lenses.reduce((n, l) => n + l.weight, 0);
  if (!lenses.length || total <= 1e-9) return null;
  return lenses.reduce((n, l) => n + l.weight * adjustedLensRate(l, inside), 0) / total;
}

/** The adjustments bearing on a sub-question, across all its lenses. */
function adjustmentsFor(id, inside) {
  if (!inside || !id) return [];
  return inside.adjustments.filter((a) => (a.sub_claim_ids || []).includes(id));
}

/** An adjustment's signed contribution. Noise moves the number by zero, by definition. */
function signedAdjustment(a) {
  if (a.is_noise) return 0;
  if (a.direction === "up") return a.magnitude;
  if (a.direction === "down") return -a.magnitude;
  return 0;
}

const CONFIDENCE_RANK = { low: 1, medium: 2, high: 3 };

/** A claim is graded by its strongest source, so an extra thin one changes nothing. */
function claimSupport(sources) {
  if (!sources || !sources.length) return "";
  return sources.reduce(
    (best, s) =>
      (CONFIDENCE_RANK[s.confidence] || 0) > (CONFIDENCE_RANK[best] || 0)
        ? s.confidence
        : best,
    "",
  );
}

/**
 * Which search returned a cited URL, joined against what the tools actually recorded.
 *
 * This is the one thing a reader cannot get from the citation alone — the agent asserts
 * the URL, the tool records the query that produced it, and only the two together answer
 * "which search found this base rate".
 */
function sourcesSeenIndex(run) {
  const by = {};
  for (const ev of run.searchLog || []) {
    if (ev.type === "source" && ev.payload.url) by[ev.payload.url] = ev.payload;
  }
  return by;
}

/** The graded sources behind a lens, which live on its evidence blocks. */
function lensSources(lens) {
  return (lens.evidence || []).map((e) => e.source).filter(Boolean);
}

/**
 * Short title per principle number, for the chips on stage headers.
 *
 * A bare "P7" is an index into a document the reader doesn't have open. The number
 * stays (it's how the code and specs refer to them) but never travels alone.
 * `spec/superforecasting_methodology.md` is the source of truth for the wording.
 */
const PRINCIPLE_TITLES = {
  1: ["Fermi-ize", "Break the question into 3–5 sub-claims you could argue about separately."],
  2: ["Knowable vs judgment", "Say which sub-claims have a lookupable base rate and which need an estimate."],
  3: ["Resolution criteria", "The question must be adjudicable as written."],
  4: ["Outside view first", "Find the base rate before reasoning about what makes this case special."],
  5: ["Inside view second", "Case specifics adjust the base rate; they never replace it."],
  6: ["Regression to the mean", "The final number must follow from the base rate plus the stated adjustments."],
  7: ["Dragonfly eye", "Use several reference classes. Disagreement between them is information, not noise."],
  8: ["Granularity", "Use the number the arithmetic gives you — 0.63, not a comfortable 0.60."],
  9: ["Signal vs noise", "If the opposite evidence would change nothing, it is noise and must move the number by zero."],
  10: ["Frequent small updates", "Revise incrementally as evidence arrives; large swings usually mean overconfidence."],
  11: ["Bayesian updating", "The probability must move the direction the stated likelihood ratios imply."],
  12: ["Under/over-reaction", "A big jump gets verified rather than forbidden; evidence that moves nothing is under-reaction."],
  13: ["Post-mortem", "Review resolved forecasts for what the reasoning got wrong."],
  14: ["Disconfirming evidence", "Look for what would prove you wrong before you settle, not after."],
  15: ["Bias checklist", "Address confirmation, availability, narrative, scope insensitivity, and anchoring."],
  16: ["Calibration over boldness", "A well-calibrated 60% beats a miscalibrated 90%. Extremes are argued for, not asserted."],
};

/** `P7 dragonfly eye`, with the full line on hover. Never a bare number. */
/**
 * A principle chip. Hovering one highlights every other mention of the same principle
 * and opens the drawer to it, so "P7" is answerable without leaving the page.
 *
 * `data-p` is the whole mechanism: the highlight is a CSS sibling rule keyed on it, and
 * the drawer scrolls to the row with the matching attribute. No backend involvement —
 * the principle text is static, and shipping it per check was the largest single thing
 * the old projection layer did.
 */
const principleChip = (n) => {
  const entry = PRINCIPLE_TITLES[n];
  const label = entry ? `P${n} ${entry[0]}` : `P${n}`;
  return h("span.chip.pchip", {
    "data-p": String(n),
    title: entry ? `P${n} — ${entry[0]}. ${entry[1]}` : `P${n}`,
    onMouseEnter: () => setPrincipleFocus(n),
    onMouseLeave: () => setPrincipleFocus(null),
    onClick: (e) => { e.stopPropagation(); openPrinciples(n); },
  }, label);
};

/**
 * Highlight every mention of a principle at once.
 *
 * Done by stamping the root rather than re-rendering: the trail is rebuilt several
 * times a second while a run streams, and a hover that triggered a full render would
 * fight the stream for frames.
 */
function setPrincipleFocus(n) {
  document.documentElement.setAttribute("data-focus-p", n == null ? "" : String(n));
}

/** Open the principles drawer, scrolled to one principle. */
function openPrinciples(n) {
  state.principlesOpen = true;
  state.principleAt = n ?? null;
  scheduleRender();
}

/** The reference the P-chips point at. Static — nothing here comes from a run. */
function renderPrinciplesDrawer() {
  if (!state.principlesOpen) return null;
  return h("div.drawer-scrim", { onClick: () => setState({ principlesOpen: false }) },
    h("aside.drawer", { onClick: (e) => e.stopPropagation() },
      h("div.drawer-head", {},
        h("h2", {}, "The sixteen principles"),
        h("div.spacer", {}),
        h("button.btn.tiny.ghost", { onClick: () => setState({ principlesOpen: false }) }, "Close")),
      h("p.dim", {}, "What the agent system is held to. Every check in a run reports the "
        + "principle it enforces; these are what those numbers mean."),
      h("div.plist", {},
        Object.entries(PRINCIPLE_TITLES).map(([n, [title, text]]) =>
          h("div.prow", {
            "data-p": n,
            class: String(state.principleAt) === n ? "on" : "",
          },
            h("div.evhead", {},
              h("span.chip.pchip", { "data-p": n }, `P${n}`),
              h("strong", {}, title)),
            h("div.dim", {}, text))))));
}

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

// ---------- markdown ----------
//
// The agents write prose, and prose from a model arrives with markdown in it. `h()`
// appends text nodes, so `**bold**` reached the page as five characters and an asterisk
// problem. This is the smallest renderer that fixes that honestly.
//
// **Escape first, always.** Everything below runs on agent-authored text, which is
// untrusted: it is assembled from search results the model read. `md()` therefore escapes
// the whole string before a single markdown rule runs, so the only tags that can ever
// reach `innerHTML` are the ones generated here. Adding a rule that emits an attribute —
// a link, an image — means escaping that attribute separately; the current grammar
// deliberately has none.

const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/** Inline spans only: bold, italic, code. Runs on already-escaped text. */
function mdInline(escaped) {
  return escaped
    // `code` first — its content must not then be read as emphasis.
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
}

/**
 * Agent prose as HTML: paragraphs, `-` and `1.` lists, and inline emphasis.
 *
 * Blocks are split on blank lines, matching how the models actually write. A run of list
 * items becomes one list; anything else becomes a paragraph with single newlines kept as
 * breaks, because a model writing a numbered list with single newlines is common and
 * collapsing it into one run-on line is how this looked before.
 */
function mdToHtml(text) {
  if (!text) return "";
  return String(text)
    .split(/\n\s*\n/)
    .map((block) => {
      const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
      if (!lines.length) return "";

      const ordered = lines.every((l) => /^\d+[.)]\s+/.test(l));
      const bulleted = lines.every((l) => /^[-*•]\s+/.test(l));
      if ((ordered || bulleted) && lines.length > 1) {
        const tag = ordered ? "ol" : "ul";
        const items = lines
          .map((l) => l.replace(/^(\d+[.)]|[-*•])\s+/, ""))
          .map((l) => `<li>${mdInline(escapeHtml(l))}</li>`)
          .join("");
        return `<${tag}>${items}</${tag}>`;
      }
      return `<p>${mdInline(escapeHtml(block.trim())).replace(/\n/g, "<br>")}</p>`;
    })
    .join("");
}

/** Agent prose as a rendered element. The one sanctioned route to `innerHTML`. */
const prose = (text, cls = "md") => h(`div.${cls}`, { html: mdToHtml(text) });

/** Markdown markers stripped rather than rendered — for one-line clipped contexts. */
const plain = (text) =>
  String(text || "").replace(/[*`_]/g, "").replace(/^\s*(\d+[.)]|[-•])\s+/gm, "");

const pct = (p) => (p === null || p === undefined ? "—" : `${Math.round(p * 100)}%`);
const domainOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u || ""; } };

/** True only for an absolute http(s) URL. */
const isExternal = (u) => {
  try { const p = new URL(u); return p.protocol === "http:" || p.protocol === "https:"; }
  catch { return false; }
};

/**
 * An external link. `noopener noreferrer` because every one of these is untrusted.
 *
 * Anything that is not an absolute http(s) URL renders as plain text. A relative href
 * would resolve against this app's own origin — a citation that looks real and goes
 * nowhere is worse than no link at all. The backend drops these too; this is the
 * second line, since saved records predate that validator.
 */
const link = (url, text) =>
  isExternal(url)
    ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text)
    : h("span", {}, text);

/**
 * What to call a source in the UI.
 *
 * A raw URL is not a name. The model is asked for a human label, but when it hands back
 * a link instead, show the domain rather than 90 characters of redirect payload.
 */
const sourceLabel = (s) => {
  // The fetched page title wins: it's recorded by the tool, not asserted by the model.
  const title = (s.title || "").trim();
  if (title && !title.startsWith("/")) return title;
  const name = (s.source || "").trim();
  const looksLikeLink = isExternal(name) || name.startsWith("/") || name.includes("?url=");
  if (name && !looksLikeLink) return name;
  // Prefer a real domain; failing that say so, rather than printing 90 characters of
  // redirect payload as if it were the name of a publication.
  if (isExternal(s.url)) return domainOf(s.url);
  if (isExternal(name)) return domainOf(name);
  return "unnamed source";
};

/**
 * How strongly a source, or a claim's strongest source, supports it.
 *
 * Colour rather than a bare word because the point is to make thin evidence visible
 * at a glance. It says nothing about whether the probability is right.
 */
const supportChip = (v) =>
  h("span.chip", { class: { high: "for", medium: "warn", low: "against" }[v] || "" },
    v ? `${v} support` : "ungraded");

/**
 * A disclosure whose open state survives a re-render.
 *
 * Native `<details>` cannot be used for anything a user opens mid-run: `render()`
 * rebuilds the whole tree on every event, so the element — and its `open` — is thrown
 * away several times a second while a run streams.
 */
const disclosure = (key, summary, body, dflt = false, bodyAttrs = {}) => {
  // `state.opened`, not `state.collapsed` — the stage accordion stores the inverse
  // (truthy means hidden), and one map meaning both things is a bug waiting to happen.
  //
  // `dflt` applies only while the key is ABSENT. A column's live tail wants to be open
  // while it researches and closed once its finding lands, but writing that on each
  // event would stomp a reader who deliberately collapsed it. Read as a default and the
  // first click makes their choice permanent.
  const open = key in state.opened ? state.opened[key] : dflt;
  return h("div.disclose", {},
    h("button.disclose-t", { type: "button", "aria-expanded": String(open),
      onClick: () => { state.opened[key] = !open; scheduleRender(); } },
      h("span.caret", {}, open ? "▾" : "▸"), summary),
    open ? h("div.work", bodyAttrs, body()) : null);
};

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
  editingBacklogId: null,  // set while `review` is editing a queued question, not a draft
  collapsed: {},           // stageKey -> true (stage group is HIDDEN)
  opened: {},              // disclosureKey -> true (disclosure is SHOWN)
  trailMissing: {},        // runId -> true, once hydration has been tried and failed
  toast: null,
  busy: false,
  principlesOpen: false,   // the P-chip reference drawer
  principleAt: null,       // which row it opened to
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

/** Whether the critic actually reached a verdict, or merely failed to clear the criteria.
 *
 *  The backend degrades to a critique with no ambiguities and the author's own text back
 *  as the "rewrite" when the critic hits its search wall — better than a 500 that throws
 *  away the parsed draft, but it is not a finding, and the copy below must not assert one.
 *  A real critique that blocks a run always offers criteria different from what was typed. */
function critiqueFoundSomething() {
  const c = state.critique;
  if (!c) return false;
  const rewritten = (c.suggested_criteria || "").trim() !== ((state.fields || {}).resolution_criteria || "").trim();
  return c.ambiguities.length > 0 || rewritten;
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
    // Running a queued question consumes it. Leaving the entry behind invites a second
    // click that spends another of the five slots on the same forecast.
    const backlog = state.editingBacklogId
      ? state.backlog.filter((b) => b.id !== state.editingBacklogId)
      : state.backlog;
    if (state.editingBacklogId) persist(BACKLOG_KEY, backlog);
    setState({ phase: "view", openId: summary.id, busy: false, draftText: "", fields: null,
               critique: null, backlog, editingBacklogId: null });
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
  setState({ backlog, phase: "draft", draftText: "", fields: null, critique: null,
             editingBacklogId: null });
  window.scrollTo(0, 0);
}

/**
 * Save edits back onto a queued question.
 *
 * Replaced in place rather than unshifted: the queue order is the reader's, and editing
 * the wording of a question they parked last week is not a reason to move it to the front.
 */
function onSaveBacklog() {
  const edited = runFieldsFrom(state.fields);
  const backlog = state.backlog.map((b) =>
    (b.id === state.editingBacklogId ? { ...b, ...edited } : b));
  persist(BACKLOG_KEY, backlog);
  setState({ backlog, phase: "draft", draftText: "", fields: null, critique: null,
             editingBacklogId: null });
  toast("Saved to the backlog.");
}

/**
 * Open a queued question in the review form.
 *
 * No critique travels with it: `CriteriaCritique` is a one-pass P3 read of the text the
 * reader originally typed, and re-running it against edited fields would spend an agent
 * call to re-answer a question nobody asked again. `isResolvable()` returns true with no
 * critique, so the form is unblocked.
 */
function onEditBacklog(id) {
  const item = state.backlog.find((b) => b.id === id);
  if (!item) return;
  setState({ phase: "review", fields: { ...item }, critique: null,
             applied: false, dismissed: false, editingBacklogId: id });
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
  const suggested = Math.min(MAX_SEARCH_DEPTH, Math.max(current + 1, current * 2));
  const depth = window.prompt(
    `Search depth to resume with (higher = more tool calls per research step).\n`
    + `Currently ${current}. Maximum ${MAX_SEARCH_DEPTH}.`,
    String(suggested),
  );
  if (depth === null) return;

  const asked = Number(depth);
  if (!Number.isFinite(asked) || asked < 1) return toast("Search depth must be a number.");
  // Clamp here rather than letting the server 422: the error text that got the reader
  // to this prompt tells them to raise the depth, so bouncing the raise is a poor answer.
  const wanted = Math.min(MAX_SEARCH_DEPTH, Math.round(asked));
  if (wanted !== Math.round(asked)) toast(`Search depth capped at ${MAX_SEARCH_DEPTH}.`);

  try {
    const summary = await resumeRun(runId, wanted);
    if (run) {
      run.summary = { ...run.summary, ...summary };
      run.detach = null;
    }
    // Re-attach from where the frames stopped: the resumed run continues the same
    // sequence, so replaying from the start would duplicate the whole trail. `keep`
    // because that also means the server will not re-send what already ran.
    attachRun(summary, (run?.lastSeq ?? 0) + 1, true);
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
    // Clear the edit target too: running the entry the review form was open on leaves
    // `editingBacklogId` pointing at a row that no longer exists.
    setState({ backlog, phase: "view", openId: summary.id,
               editingBacklogId: state.editingBacklogId === id ? null : state.editingBacklogId });
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

/**
 * Register a run and open its stream. Idempotent — reopening is a no-op.
 *
 * `keep` reattaches to a run whose trail is already on screen, as resume does. The
 * stream continues from `fromSeq` rather than replaying, so the server will never
 * re-send the earlier stages — throwing them away here loses them for good, which is
 * how resuming used to blank the base rates it had already found.
 */
function attachRun(summary, fromSeq = 0, keep = false) {
  const existing = state.runs[summary.id];
  if (existing && existing.detach) return existing;

  const local = keep && existing
    ? { ...existing, summary, lastSeq: Math.max(existing.lastSeq ?? 0, fromSeq), detach: null }
    : {
        summary,
        stages: [],
        // The typed objects the graph emits, keyed by what they are. Every renderer
        // reads from here rather than from the event that delivered them.
        models: {},
        searchLog: [],
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
 * Two kinds of event arrive, and they are handled differently on purpose.
 *
 * **Stage results** carry a whole typed object — `decompose` is a `Decomposition`,
 * `outside` an `OutsideView`. They land in `run.models`, and the renderers read from
 * there. Nothing is flattened on arrival, so adding a field to a model shows up in the
 * UI without a wire format to change first.
 *
 * **Live progress** — `query`, `source`, `thought`, `exhausted` — is inherently
 * incremental and has no whole-model equivalent. It routes into the column card it is
 * tagged with, which is what keeps a four-minute research row legible while it works
 * rather than blank until its barrier.
 *
 * A stage group is keyed by `stage + attempt`, which is what makes the synthesize retry
 * render as its own "attempt 2" card instead of overwriting attempt 1.
 */
function applyEvent(runId, ev) {
  const run = state.runs[runId];
  if (!run) return;
  run.lastSeq = Math.max(run.lastSeq, ev.seq);
  run.models = run.models || {};

  switch (ev.type) {
    case "stage": {
      const key = `${ev.payload.stage}-${ev.payload.attempt}`;
      if (!run.stages.some((s) => s.key === key)) {
        const group = { key, stage: ev.payload.stage, attempt: ev.payload.attempt,
                        items: [], columns: null, columnOrder: [] };
        openColumns(run, group);
        run.stages.push(group);
      }
      run.summary = { ...run.summary, stage: ev.payload.stage, attempt: ev.payload.attempt,
                      stage_index: STAGE_ORDER.indexOf(ev.payload.stage) + 1 };
      return;
    }
    case "stage_end":
      return; // The header already moved on; nothing to draw for a close.

    // ---- whole typed objects ----
    case "decompose":
      run.models.decomposition = ev.payload;
      return;
    case "lenses":
      run.models.lenses = ev.payload.lenses || [];
      closeColumns(run, "lenses");
      return;
    case "outside":
      run.models.outside = ev.payload;
      closeColumns(run, "outside");
      return;
    case "inside":
      run.models.inside = ev.payload;
      closeColumns(run, "inside");
      return;
    case "synth":
      (run.models.drafts = run.models.drafts || []).push(ev.payload);
      run.models.forecast = ev.payload;
      return;
    case "critique":
      run.models.violations = ev.payload.violations || [];
      return;

    case "result":
      run.result = ev.payload;
      run.summary = { ...run.summary, forecast_id: ev.payload.forecast_id };
      saveResult(runId, ev.payload, run);
      return;
    case "resume":
      // Needs its own case: the fall-through below appends to the *last stage group*,
      // and a resume arrives before the resumed step has emitted its `stage`.
      run.summary = { ...run.summary, status: "running",
                      max_iterations: ev.payload.max_iterations ?? run.summary.max_iterations };
      run.stages.push({ key: `resume-${ev.seq}`, stage: "resume", attempt: 1, items: [ev] });
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
  if (ev.type === "source" || ev.type === "query") {
    // A flat log of everything the tools recorded, kept so a citation can be joined
    // back to the search that produced it. See `sourcesSeenIndex`.
    (run.searchLog = run.searchLog || []).push(ev);
  }

  const bucket = ev.sub_claim == null ? group : columnFor(group, ev.sub_claim);
  if (!bucket) return;

  if (ev.type === "exhausted") {
    bucket.exhausted = true;
    bucket.done = true;
    return;
  }

  // Consecutive thought deltas belong to one paragraph — the server coalesces on an
  // 80ms timer, which still leaves several frames inside a single stretch of prose.
  // Merged within a column: two columns narrating at once must not run together.
  const last = bucket.items[bucket.items.length - 1];
  if (ev.type === "thought" && last && last.type === "thought") {
    last.payload = { ...last.payload, delta: last.payload.delta + ev.payload.delta };
    return;
  }
  bucket.items.push(ev);
}

/**
 * Open one card per column at the top of a research row, before any agent runs.
 *
 * Derived from the decomposition the previous stage already delivered, which is why the
 * backend no longer sends a `column` event: everything this needs is on a model the
 * client is holding. Without the cards the row is blank until its barrier, which for
 * four concurrent searches is several minutes of nothing.
 *
 * `researching` is derived rather than asserted — a `judgment` column gets a card that
 * says there is no base rate to look up, and never enters research. It still exists,
 * because a column that vanishes from a row looks like a bug rather than an answer.
 */
function openColumns(run, group) {
  // Deliberately not "lenses": that stage shows populations *before* any rate exists,
  // and a column card reads rates off `models.outside`. Opening cards there would
  // back-fill measurements into the step whose entire purpose is not having them yet.
  if (!["outside", "inside"].includes(group.stage)) return;
  const d = run.models.decomposition;
  if (!d) return;

  group.columns = {};
  group.columnOrder = [];
  for (const s of d.sub_claims) {
    if (!s.id) continue;
    const anchor =
      group.stage === "inside" ? subClaimRate(s.id, run.models.outside) : null;
    const lenses = lensesFor(s.id, run.models.outside);
    group.columns[s.id] = {
      id: s.id,
      question: s.question,
      knowability: s.knowability,
      rationale: s.rationale,
      p: s.probability,
      anchor,
      lenses,
      // The inside row adjusts FROM rates the row above it measured. No lens means
      // nothing to adjust, which is P5's premise.
      researching:
        group.stage === "inside" ? lenses.length > 0 : s.knowability === "researchable",
      items: [],
      done: false,
      exhausted: false,
    };
    group.columnOrder.push(s.id);
  }
}

/** Mark a research row's cards finished once its barrier has delivered the model. */
function closeColumns(run, stage) {
  const group = [...run.stages].reverse().find((g) => g.stage === stage);
  if (!group || !group.columns) return;
  for (const id of group.columnOrder) group.columns[id].done = true;
}

/**
 * The column a tagged event belongs to, created on the spot if it arrives first.
 *
 * A stray should be impossible — the cards open when the stage does — but dropping an
 * event to keep an invariant is the wrong trade when the invariant is only about
 * ordering.
 */
function columnFor(group, id) {
  group.columns = group.columns || {};
  if (!group.columns[id]) {
    group.columns[id] = { id, question: "", items: [], done: false, exhausted: false };
    group.columnOrder.push(id);
  }
  return group.columns[id];
}

function saveResult(runId, payload, run) {
  const f = payload.forecast || {};
  const record = {
    id: runId,
    forecast_id: payload.forecast_id,
    question: f.question,
    probability: f.probability,
    anchor: run && run.models.outside ? run.models.outside.aggregate_base_rate : null,
    reasoning: f.reasoning,
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
        // frames stopped rather than replaying the whole timeline — and `keep` the
        // trail, since not replaying means nothing will send those stages again.
        if (!isTerminal(summary.status)) {
          attachRun(summary, local.lastSeq + 1, true);
          changed = true;
        }
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

/**
 * Renderers for the live-progress events — the ones that are inherently incremental and
 * have no whole-model equivalent.
 *
 * Stage *results* are not here. Those arrive as whole typed objects and are drawn by
 * `renderStageBody` from `run.models`, which is why this table is a fraction of its
 * former size: it used to hold a renderer per flattened field the backend invented.
 */
const EVENT_RENDERERS = {
  thought: (p) =>
    h("div.ev.thought", {}, p.delta, h("span.caret", {}, "\u258d")),

  query: (p) =>
    h("div.ev.query", {},
      h("span.chip", {}, p.tool),
      h("span.dim", {}, `\u201c${p.q}\u201d`)),

  source: (p) =>
    h("div.ev.source", {},
      h("span", { style: `width:6px;height:6px;border-radius:50%;background:var(--pv-text-3);flex:none;margin-top:6px` }),
      h("div", {},
        h("div", {}, link(p.url, p.title || domainOf(p.url))),
        h("div.micro", {}, domainOf(p.url), p.published_date ? ` \u00b7 ${p.published_date.slice(0, 10)}` : ""))),

  error: (p) =>
    h("div.ev.route", {},
      prose(p.message),
      p.hint ? h("div", { style: "margin-top:5px" }, prose(p.hint)) : null,
      p.resumable
        ? h("div.micro", { style: "margin-top:6px" },
            "Resuming re-runs only the step that failed \u2014 everything before it keeps "
            + "the result it was already paid for.")
        : null),

  resume: (p) =>
    h("div.ev.brief", {},
      h("div.micro", {}, "Resumed"),
      h("div.dim", {}, `Search depth ${p.max_iterations}. Earlier stages kept.`)),

  truncated: (p) =>
    h("div.ev.truncated", {}, `${p.count} earlier events were dropped from the buffer.`),

  // Drawn by the column card that owns it, not inline. Registered so `renderTrail`'s
  // `if (!renderer) return null` does not silently swallow one outside a card.
  exhausted: () => null,
};

/**
 * One column of the grid: a sub-question, end to end, at one stage.
 *
 * Opened by a `column` event at the top of the row — before any agent starts — so a row
 * that spends four minutes on four concurrent searches is legible for all four of them
 * rather than blank until the barrier.
 */
function renderColumnCard(run, group, col) {
  const key = `${group.key}-${col.id}`;
  const log = col.items;
  const isInside = group.stage === "inside";
  const lenses = lensesFor(col.id, run.models.outside);
  const inside = isInside ? run.models.inside : null;

  // The sub-question's rate: its lenses blended by relevance. On the inside row the
  // lenses have already been moved by their own modifiers.
  const rate = subClaimRate(col.id, run.models.outside, inside);

  // One clipped line of whatever the agent is doing right now.
  const live = !col.done && log.length ? log[log.length - 1] : null;
  const liveText = live
    ? (live.type === "thought" ? live.payload.delta
       : live.type === "query" ? `${live.payload.tool} \u00b7 ${live.payload.q}`
       : live.payload.title || live.payload.domain || "")
    : "";

  return h("div.colcard", { class: col.done ? "" : "busy" },
    h("div.evhead", {},
      h("span.num-strong", { style: rate == null ? "color:var(--pv-text-3)" : "" },
        rate == null ? "\u2014" : pct(rate)),
      col.id ? h("span.micro", {}, col.id) : null,
      h("div.spacer", {}),
      col.exhausted ? h("span.chip.warn", {}, "budget spent") : null,
      col.knowability
        ? h("span.chip", { class: col.knowability === "researchable" ? "for" : "" },
            col.knowability)
        : null),
    h("div", {}, col.question),
    isInside && col.anchor != null && rate != null && Math.abs(rate - col.anchor) > 1e-9
      ? h("div.micro", {}, `from ${pct(col.anchor)}`)
      : null,

    !col.researching && !log.length
      ? h("div.micro", {},
          col.knowability === "researchable"
            ? "no population measured \u2014 nothing was researched for this"
            : "judgment \u2014 no base rate to look up")
      : null,

    live ? h("div.collive", {}, plain(liveText)) : null,

    log.length
      ? disclosure(`col-log-${key}`,
          h("span.micro", {}, `${log.length} search step${log.length === 1 ? "" : "s"}`),
          () => log.map((ev) => EVENT_RENDERERS[ev.type]?.(ev.payload, ev)).filter(Boolean),
          !col.done,
          { "data-scroll": `col-log-${key}`, class: "coltail" })
      : null,

    lenses.length
      ? h("div.lenses", {},
          lenses.map((l, i) => renderLens(run, l, inside, `${key}-${i}`)))
      : null);
}

/**
 * One population: what it is, what it measured, what moved it.
 *
 * The whole methodology is legible here — a rate that is `hits/n` over cases you can
 * open, modifiers that move only this population, and a weight that admits it is a
 * judgment. It is the one card worth reading closely.
 */
function renderLens(run, lens, inside, key) {
  const base = lensRate(lens);
  const moves = adjustmentsForLens(lens, inside);
  const adjusted = adjustedLensRate(lens, inside);
  const moved = adjusted - base;
  const seen = sourcesSeenIndex(run);
  const queries = [...new Set(lensSources(lens)
    .map((sc) => seen[sc.url] && seen[sc.url].query)
    .filter(Boolean))];

  return h("div.lens", {},
    h("div.evhead", {},
      h("span.num-strong", {}, pct(base)),
      moves.length
        ? h("span.micro", {},
            `\u2192 ${pct(adjusted)}  ${moved > 0 ? "+" : ""}${Math.round(moved * 100)} pts`)
        : null,
      h("div.spacer", {}),
      lensSources(lens).length
        ? supportChip(claimSupport(lensSources(lens)))
        : h("span.chip.for", {}, "counted")),
    h("div", {}, lens.name),
    // What the rate is counted from. This is the number that used to be an unverified
    // "N=50"; it is now the denominator of an arithmetic a check re-derives.
    h("div.micro", {}, lensEvidenceSummary(lens)),
    h("div.minibar", {}, h("i", { style: `width:${Math.min(100, base * 100)}%` })),
    lens.population ? h("div.dim", {}, lens.population) : null,
    queries.length ? h("div.micro", {}, `searched: ${queries.join(" \u00b7 ")}`) : null,

    // The one number nothing can check, so it shows its argument.
    lens.weight_rationale
      ? disclosure(`w-${key}`,
          h("span.micro", {}, `relevance ${lens.weight.toFixed(2)} \u2014 why`),
          () => prose(lens.weight_rationale, "dim md"))
      : h("div.micro", {}, `relevance ${lens.weight.toFixed(2)}`),

    moves.length
      ? disclosure(`m-${key}`,
          h("span.micro", {}, `${moves.length} modifier${moves.length === 1 ? "" : "s"}`),
          () => moves.map((a, i) => renderAdjustment(a, `${key}-${i}`)),
          true)
      : null,

    lensSources(lens).length
      ? renderSources(`src-${key}`, lensSources(lens), seen)
      // A counted lens has no citation by design — the cases below ARE its evidence,
      // and `check_base_rate_derivation` audits them against the count.
      : h("div.micro", {}, "counted directly \u2014 the cases below are the evidence"),

    (lens.analogs || []).length
      ? disclosure(`an-${key}`,
          h("span.micro", {}, `${lens.analogs.length} case${lens.analogs.length === 1 ? "" : "s"} counted`),
          () => lens.analogs.map((a) =>
            h("div.srcrow", {},
              h("span.chip", { class: a.outcome >= 1 ? "for" : "against" },
                a.outcome >= 1 ? "yes" : "no"),
              h("div", {},
                prose(a.description),
                a.relevance ? prose(a.relevance, "dim md") : null))))
      : null);
}

/** One signed move away from a column's base rate. */
function renderAdjustment(a, key) {
  const delta = signedAdjustment(a);
  return h("div.ev.adj", {},
    h("div.evhead", {},
      h("span.num-strong", { class: delta > 0 ? "up" : delta < 0 ? "down" : "" },
        `${delta > 0 ? "+" : ""}${Math.round(delta * 100)} pts`),
      a.is_noise ? h("span.chip", {}, "noise") : null,
      h("div.spacer", {}),
      supportChip(claimSupport(a.sources))),
    prose(a.evidence),
    a.flip_test
      ? h("div.dim", {}, h("span.micro", {}, "flip test"), prose(a.flip_test, "md"))
      : null,
    addresses(a.sub_claim_ids),
    renderSources(`adjsrc-${key}`, a.sources));
}

/**
 * Which decomposed sub-claim this base rate or adjustment answers.
 *
 * Empty is meaningful, not missing: a reference class can legitimately speak to the
 * whole question rather than one part of it.
 */
const addresses = (ids) =>
  h("div.micro", {}, ids && ids.length ? `addresses ${ids.join(", ")}` : "addresses the whole question");

/**
 * One reference class, inside the sub-claim it was found for.
 *
 * Analogs nest here. They are a child collection on `ReferenceClass` but used to be
 * emitted as flat sibling events, so which class an analog belonged to was carried only
 * by arrival order — a convention nothing wrote down and the UI could not show.
 */
function renderClass(run, c, key) {
  // Which search produced each cited URL. The agent asserts the URL; the tool records
  // the query — only the join answers "which search found this base rate".
  const seen = sourcesSeenIndex(run);
  const queries = [...new Set((c.sources || [])
    .map((s) => seen[s.url] && seen[s.url].query)
    .filter(Boolean))];

  return h("div.refcard", {},
    h("div.evhead", {},
      h("span.num-strong", {}, pct(c.base_rate)),
      h("span.micro", {}, `n=${c.sample_size}`),
      c.weight === undefined ? null : h("span.micro", {}, `weight ${c.weight.toFixed(2)}`),
      h("div.spacer", {}),
      supportChip(claimSupport(c.sources))),
    h("div", {}, c.name),
    h("div.minibar", {}, h("i", { style: `width:${Math.min(100, c.base_rate * 100)}%` })),
    queries.length ? h("div.micro", {}, `searched: ${queries.join(" · ")}`) : null,
    renderSources(`src-${key}`, c.sources, seen),
    (c.analogs || []).length
      ? disclosure(`analogs-${key}`,
          h("span.micro", {}, `${c.analogs.length} analog${c.analogs.length === 1 ? "" : "s"}`),
          () => c.analogs.map((a) =>
            h("div.srcrow", {},
              h("span.chip", { class: a.outcome >= 1 ? "for" : "against" },
                a.outcome >= 1 ? "yes" : "no"),
              h("div", {},
                prose(a.description),
                a.relevance ? prose(a.relevance, "dim md") : null))))
      : null);
}

/** The graded sources behind one claim, collapsed until asked for. */
function renderSources(key, sources, seen) {
  if (!sources || !sources.length) {
    return h("div.micro", {}, "no sources — judgment call");
  }
  const label = `${sources.length} source${sources.length === 1 ? "" : "s"}`;
  return disclosure(key, h("span.micro", {}, label), () =>
    sources.map((s) => {
      const record = seen && seen[s.url];
      return h("div.srcrow", {},
        supportChip(s.confidence),
        h("div", {},
          h("div", {}, link(s.url, sourceLabel(s))),
          s.note ? prose(s.note, "dim md") : null,
          record && record.query ? h("div.micro", {}, `found by: ${record.query}`) : null,
          // A cited URL no search returned is what `check_citations` fails a forecast
          // for. Saying so here means a reader sees it without running the check.
          seen && !record ? h("span.chip.against", {}, "not retrieved") : null));
    }));
}


// ---------- render ----------

function render() {
  const root = document.getElementById("root");
  document.documentElement.setAttribute("data-theme", state.theme);

  const focus = captureFocus();
  const scrolls = captureScrolls();
  // `replaceChildren` is raw DOM and does NOT skip nulls the way `h` does — it
  // stringifies them, which is how a literal "null" ended up on the page.
  root.replaceChildren(
    ...[
      renderHeader(),
      h("div.shell", {}, renderRail(), renderMain()),
      renderPrinciplesDrawer(),
      state.toast ? h("div.toast", {}, state.toast) : null,
    ].filter(Boolean),
  );
  restoreFocus(focus);
  restoreScrolls(scrolls);
}

/**
 * Remember where each scrollable log was, and whether it was following the tail.
 *
 * The focus pair above solves this for inputs; a column's live search log needs the
 * same treatment for the same reason — the whole tree is rebuilt several times a second
 * while three columns stream, and a region that resets to the top every frame cannot be
 * read at all.
 *
 * "Pinned" means within 8px of the bottom, which is the difference between a reader
 * watching the tail and a reader who scrolled up to look at something. Yanking the
 * second one back down is the single worst thing a live log can do, so the distinction
 * is worth the two lines.
 */
function captureScrolls() {
  const out = {};
  document.querySelectorAll("[data-scroll]").forEach((el) => {
    out[el.getAttribute("data-scroll")] = {
      top: el.scrollTop,
      pinned: el.scrollHeight - el.scrollTop - el.clientHeight < 8,
    };
  });
  return out;
}

function restoreScrolls(saved) {
  document.querySelectorAll("[data-scroll]").forEach((el) => {
    const s = saved[el.getAttribute("data-scroll")];
    // No entry means the region is new — a log that just opened starts at its tail.
    el.scrollTop = !s || s.pinned ? el.scrollHeight : s.top;
  });
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
    h("button.btn.tiny.ghost", { onClick: () => openPrinciples(null) }, "Principles"),
    // No web search is not a smaller version of the same forecast — it is one built on
    // Wikipedia alone. Worth one word in the header rather than a surprise in the trail.
    serverConfig.search_enabled ? null
      : h("span.chip.warn", { title: "TAVILY_API_KEY is not set. Wikipedia still works, but base rates will be thinner." },
          "no web search"),
    // Hidden entirely on a local server with no ADMIN_API_KEY — there is no token to set,
    // and offering the button implies the run will fail without one.
    serverConfig.auth_required
      ? h("button.btn.tiny.ghost", {
          onClick: () => {
            const token = window.prompt("Admin token (needed to start a run):", getAdminToken() || "");
            if (token !== null) { setAdminToken(token.trim() || null); toast(token.trim() ? "Token saved." : "Token cleared."); }
          },
        }, getAdminToken() ? "Admin ✓" : "Admin")
      : null,
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
          h("span.chip", { class: r.probability < 0.5 ? "against" : "for" }, r.probability < 0.5 ? "no" : "yes")),
        h("div", {}, r.question),
        h("div.micro", {}, r.stamp.slice(0, 10)),
        h("button.del", {
          title: "Remove from this browser — the forecast stays on the server",
          onClick: (e) => { e.stopPropagation(); onDeleteSaved(r.id); },
        }, "×"))),

    renderBacklog(),

    h("div.micro", { style: "margin-top:22px" }, "Autosaved to this browser"),
  );
}

function renderMain() {
  if (state.phase === "view") return h("main.main", {}, renderRun());
  return h("main.main", {},
    state.phase === "draft" ? renderDraft() : null,
    state.phase === "parsing" ? renderParsing() : null,
    state.phase === "review" ? renderReview() : null,
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
  const found = critiqueFoundSomething();
  const editing = !!state.editingBacklogId;
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
      h("div.evhead", {},
        h("h2", {}, editing ? "Queued question" : "Parsed from your text"), h("div.spacer", {}),
        h("button.btn.tiny", {
          onClick: () => setState({ phase: "draft", editingBacklogId: null }),
        }, editing ? "Discard changes" : "Rewrite")),
      field("Question", "question", { textarea: true }),
      field("Resolution criteria", "resolution_criteria", { textarea: true, bad: !ok }),
      h("div.grid2", {},
        field("Resolution date", "resolution_date", { type: "date" }),
        field("Category", "category", { select: true })),
      field("Resolution source", "resolution_source", { bad: !f.resolution_source }),

      h("div", { style: "display:flex;gap:8px;align-items:center;margin-top:6px" },
        h("span.micro", { style: "flex:1" },
          !ok && found ? "Blocked: the criteria have to be adjudicable before a run is worth its cost."
              : !ok ? "The resolvability check did not finish. Read the criteria yourself before spending a run on them."
              : editing ? "Editing a queued question. Saving updates the backlog; running it removes it from the queue."
              : slotsFree() <= 0 ? "All five run slots are busy. Add it to the backlog."
              : "Full graph, two clamped search tools. Expect five to eight minutes."),
        h("button.btn", { onClick: editing ? onSaveBacklog : onQueueToBacklog },
          editing ? "Save changes" : "Add to backlog"),
        h("button.btn.primary", { disabled: !ok || slotsFree() <= 0 || state.busy, onClick: onRunNow },
          state.busy ? "Starting…" : "Run now"))),

    c ? h("section.panel", {},
      h("div.evhead", {},
        h("span.micro", {}, "Resolvability"),
        h("span.chip", { class: ok ? "for" : "against" },
          ok ? "adjudicable" : found ? "not resolvable" : "not checked")),
      h("p.dim", {}, ok
        ? "Two people reading these criteria on resolution day would reach the same verdict. Cleared to run."
        : found
        ? `${c.ambiguities.length} ambiguities and ${c.missing.length} structural gaps. Two people reading this on resolution day could argue, so the forecast would not be scoreable.`
        : "The check did not finish, so nothing here has been cleared or faulted. Read the criteria yourself and keep them, or edit and read the question back again."),

      c.ambiguities.length ? h("div", {},
        h("div.micro", { style: "margin-top:12px" }, `Ambiguities · ${c.ambiguities.length}`),
        c.ambiguities.map((t, i) => h("div.ev.note", { style: "margin-top:6px" },
          h("span.micro", {}, String(i + 1)), " ", t))) : null,

      c.missing.length ? h("div", {},
        h("div.micro", { style: "margin-top:12px" }, `Missing · ${c.missing.length}`),
        c.missing.map((t) => h("div.ev.note", { style: "margin-top:6px" }, "— ", t))) : null,

      !ok ? h("div", { style: "margin-top:16px" },
        found ? h("div", {},
          h("div.micro", {}, "Suggested rewrite"),
          h("p", { style: "margin-top:6px" }, c.suggested_criteria),
          h("div.micro", {}, `Source · ${c.suggested_resolution_source}`)) : null,
        h("div", { style: "display:flex;gap:8px;margin-top:10px" },
          found ? h("button.btn.primary", { onClick: onApplyRewrite }, "Apply rewrite") : null,
          h("button.btn", { onClick: () => setState({ dismissed: true }) },
            found ? "Keep mine" : "Proceed anyway"))) : null,
    ) : null,
  );
}

/**
 * The queue, in the rail beside Running and Saved.
 *
 * It lives here rather than in the main column because it is a list you pick from, the
 * same shape as the two lists above it — and because running from it should not mean
 * leaving whatever the main column is showing.
 */
function renderBacklog() {
  const free = slotsFree();
  return h("div", { style: "margin-top:22px" },
    h("div.micro", {}, `Backlog · ${state.backlog.length}`),
    state.backlog.length === 0
      ? h("div.empty", {}, "Nothing queued. Questions you add wait here for a free run slot.")
      : h("div.micro", { style: "margin:4px 0 8px" },
          free > 0 ? `${free} of ${MAX_SLOTS} slots free` : "All slots busy — these wait"),
    state.backlog.map((b) =>
      h("div.rowcard", {
        class: state.editingBacklogId === b.id ? "on" : "",
        title: "Open and edit this question",
        onClick: () => onEditBacklog(b.id),
      },
        h("div.evhead", {},
          h("span.chip", {}, b.category),
          h("span.micro", {}, `resolves ${(b.resolution_date || "").slice(0, 10)}`)),
        h("div", {}, b.question),
        h("div", { style: "display:flex;gap:6px;margin-top:8px" },
          h("button.btn.tiny.primary", {
            disabled: free <= 0,
            title: free <= 0 ? "Every run slot is busy" : "Start this forecast",
            onClick: (e) => { e.stopPropagation(); onRunFromBacklog(b.id); },
          }, "Run forecast"),
          h("button.btn.tiny.ghost", {
            onClick: (e) => { e.stopPropagation(); onDropBacklog(b.id); },
          }, "Remove")))),
  );
}

function renderRun() {
  const run = state.runs[state.openId];
  const saved = state.saved.find((r) => r.id === state.openId);
  // A live run's `result` event carries the whole `Forecast`; a saved run carries the
  // flattened record this browser wrote. Normalise once here so the card reads one shape.
  const result = run?.result
    ? {
        probability: run.result.forecast.probability,
        reasoning: run.result.forecast.reasoning,
        violations: run.result.violations,
        anchor: run.models?.outside ? run.models.outside.aggregate_base_rate : null,
      }
    : saved;
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
    ),

    r.violations && r.violations.length
      ? h("div.ev.route", { style: "margin-top:14px" },
          `${r.violations.length} check${r.violations.length === 1 ? "" : "s"} still failing after the retry: ` +
          r.violations.map((v) => v.name).join(", "))
      : null,

    r.reasoning ? h("div", { style: "margin-top:18px" },
      h("div.micro", {}, "Reasoning"),
      prose(r.reasoning)) : null,
  );
}

/** Event types that are the research *log* rather than a finding. */
const LOG_TYPES = new Set(["thought", "query", "source"]);

/**
 * Split a stage's events into alternating runs of log-noise and findings.
 *
 * Preserves arrival order — the log stays where it happened rather than being hoisted
 * into one bucket, so a reader expanding it still sees which findings it preceded.
 */
function runsOf(items) {
  const out = [];
  for (const ev of items) {
    const log = LOG_TYPES.has(ev.type);
    const last = out[out.length - 1];
    if (last && last.log === log) last.items.push(ev);
    else out.push({ log, items: [ev] });
  }
  return out;
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
          h("div.pchips", {}, (meta.principles || []).map(principleChip)),
          h("div.spacer", {}),
          busy ? h("span.micro.pulse", {}, "working")
               // Only a fanned-out row has a count worth showing — "one agent per
               // column, and there were four". The other stages ran once.
               : (group.columnOrder || []).length
                 ? h("span.micro", {}, `${group.columnOrder.length} columns`)
                 : null),
        collapsed ? null : h("div.items", {},
          // A fanned-out row draws its columns as cards, then whatever it emitted for
          // the question as a whole below them — the anchor note, the reflect pass's
          // steel-man and bias sweep. `runsOf` still handles the untagged rows.
          (group.columnOrder || []).length
            ? h("div.cols", {},
                group.columnOrder.map((id) => renderColumnCard(run, group, group.columns[id])))
            : null,
          // What this stage produced, drawn from the typed object it delivered.
          renderStageBody(run, group),
          runsOf(group.items).map((run_) => {
            const draw = (ev) => {
              const renderer = EVENT_RENDERERS[ev.type];
              if (!renderer) return null;
              // `ev` as well as the payload: a disclosure needs a key that survives
              // re-render, and `seq` is the only stable identity an event has.
              const node = renderer(ev.payload, ev);
              if (ev.seq > animatedUpTo) node.classList.add("fresh");
              return node;
            };
            if (!run_.log) return run_.items.map(draw);
            // The raw research trail — every query issued and every URL the tool
            // returned, in arrival order. It is the audit record, not the finding, and
            // at full length it buries the base rates it produced. The attributed
            // version lives on each claim ("searched: …", per-source "found by: …").
            const n = run_.items.length;
            return disclosure(`log-${group.key}-${run_.items[0].seq}`,
              h("span.micro", {}, `${n} search step${n === 1 ? "" : "s"}`),
              () => run_.items.map(draw));
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
// Re-render once the server has said whether it wants a token and whether search is on.
// Not awaited before the first paint: the page is useful immediately, and the two things
// this changes are a header chip and a button.
loadServerConfig().then(scheduleRender);
pollRuns();
window.setInterval(pollRuns, POLL_MS);

/**
 * What a stage produced, drawn from the model it delivered rather than from events.
 *
 * The backend streams the object its agent returned and nothing more, so this is where
 * "what should a reader see for this stage" is decided — in the layer that owns layout,
 * next to the CSS it depends on.
 */
function renderStageBody(run, group) {
  const m = run.models || {};
  switch (group.stage) {
    case "decompose":
      return m.decomposition ? renderDecomposition(m.decomposition) : null;
    case "lenses":
      return m.lenses ? renderChosenLenses(m.lenses) : null;
    case "outside":
      return m.outside ? renderAnchorNote(m.outside) : null;
    case "reflect":
      return m.inside ? renderReflection(m.inside) : null;
    case "synth": {
      const draft = (m.drafts || [])[group.attempt - 1];
      return draft
        ? h("div.ev.draft", {},
            h("div.evhead", {},
              h("span.num-strong", {}, pct(draft.probability)),
              h("span.micro", {}, `Attempt ${group.attempt}`)))
        : null;
    }
    case "critique":
      return renderChecks(run, group);
    default:
      return null;
  }
}

/** P1 + P2 — the sub-claims, and how they combine. */
function renderDecomposition(d) {
  return h("div", {},
    d.sub_claims.map((s) =>
      h("div.ev.sub", {},
        h("div.evhead", {},
          h("span.chip", { class: s.knowability === "researchable" ? "for" : "" }, s.knowability),
          s.id ? h("span.micro", {}, s.id) : null,
          h("div.spacer", {}),
          h("span.micro", {}, pct(s.probability))),
        h("div", {}, s.question),
        prose(s.rationale, "dim md"))),
    h("div.ev.note", {},
      h("div.micro", {}, "chain_note"),
      prose(d.chain_note, "dim md")));
}

/**
 * P7 — the anchor, and what the classes disagreed about.
 *
 * The disagreement sentence is the half of P7 a schema cannot enforce, which is the
 * whole reason for surfacing it rather than letting it sit inside the model.
 */
function renderAnchorNote(o) {
  return h("div.ev.note", {},
    h("div.micro", {}, `aggregate_base_rate — ${pct(o.aggregate_base_rate)}`),
    prose(
      o.disagreement.trim()
      || `${o.reference_classes.length} reference classes, broadly in agreement.`,
      "dim md"));
}

/** P14 + P15 — the case against, and the bias sweep. Whole-question by construction. */
function renderReflection(i) {
  return h("div", {},
    h("div.ev.note", {},
      h("div.micro", {}, "steel_man"),
      prose(i.steel_man, "dim md")),
    h("div.ev.note", {},
      h("div.micro", {}, "what_would_change_my_mind"),
      prose(i.what_would_change_my_mind, "dim md")),
    i.bias_checks.map((b) =>
      h("div.ev.bias", {},
        h("span.chip", {}, b.bias),
        prose(b.assessment, "dim md"))));
}

/**
 * The methodology verdict for this attempt.
 *
 * Only violations cross the wire now — a check that passed says nothing a reader needs,
 * and shipping all ten of them plus a per-check evidence payload was the single largest
 * thing the old projection layer did.
 */
function renderChecks(run, group) {
  const violations = run.models.violations;
  if (!violations) return null;
  if (!violations.length) {
    return h("div.ev.check", {},
      h("span.chip.for", {}, "✓"),
      h("span", {}, "Every methodology check passed."));
  }
  const blocking = violations.filter((v) => v.blocking);
  return h("div", {},
    violations.map((v) =>
      h("div.ev.check", {},
        h("div.evhead", {},
          h("span.chip", { class: v.blocking ? "against" : "" }, v.blocking ? "✗" : "!"),
          h("span", {}, v.name),
          h("div.spacer", {}),
          principleChip(v.principle)),
        prose(v.detail, "dim md"))),
    blocking.length && group.attempt < 2
      ? h("div.ev.route", {},
          `↩ ${blocking.length} blocking violation${blocking.length === 1 ? "" : "s"}. `
          + `Routing back to Synthesize — attempt ${group.attempt + 1} of 2, with the `
          + `violation in the prompt.`)
      : null);
}
