/**
 * Typed-ish fetch wrappers and the SSE client.
 *
 * Same-origin by default because FastAPI serves this directory. `window.SF_API_URL`
 * overrides it when the page is opened from disk or a separate dev server.
 */

const API = (window.SF_API_URL || "").replace(/\/$/, "");
const ADMIN_TOKEN_KEY = "superforecaster_admin_token";

/** @returns {string|null} */
function getAdminToken() {
  try { return window.localStorage.getItem(ADMIN_TOKEN_KEY); } catch { return null; }
}

function setAdminToken(token) {
  try {
    if (token) window.localStorage.setItem(ADMIN_TOKEN_KEY, token);
    else window.localStorage.removeItem(ADMIN_TOKEN_KEY);
  } catch { /* private mode */ }
}

/**
 * Flatten whatever FastAPI put in `detail` into one line.
 *
 * A 422 from request validation carries a LIST of objects, not a string:
 *   [{type, loc: ["body","max_iterations"], msg: "Input should be <= 20", input: 25}]
 * Callers toast `detail` directly, so forwarding that verbatim renders as
 * "[object Object]" — which is exactly what a rejected search depth used to show.
 */
function detailToText(detail, status) {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        const field = Array.isArray(d?.loc) ? d.loc[d.loc.length - 1] : null;
        const msg = d?.msg || d?.type;
        if (!msg) return null;
        return field ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  if (detail && typeof detail === "object") {
    const msg = detail.msg || detail.message;
    if (msg) return String(msg);
  }
  return `HTTP ${status}`;
}

/**
 * @throws {{status:number, detail:string}} on any non-2xx — callers branch on
 *   `status` (429 means every run slot is busy, 403 means no admin token).
 *   `detail` is ALWAYS a string; see `detailToText`.
 */
async function req(path, { method = "GET", body, admin = false } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (admin) {
    const token = getAdminToken();
    if (!token) throw { status: 403, detail: "Admin token not set." };
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(API + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (res.status === 204) return null;

  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }

  if (!res.ok) {
    throw { status: res.status, detail: detailToText(data && data.detail, res.status) };
  }
  return data;
}

/** POST /questions/draft — freeform text into {parsed, critique}. */
const draftQuestion = (text) =>
  req("/questions/draft", { method: "POST", body: { text } });

/** POST /runs. Throws {status:429} when every slot is busy. */
const createRun = (fields) =>
  req("/runs", { method: "POST", body: fields, admin: true });

const listRuns = () => req("/runs");

/** POST /runs/{id}/resume — re-runs only the node that failed. */
const resumeRun = (id, maxIterations) =>
  req(`/runs/${id}/resume`, {
    method: "POST",
    body: maxIterations ? { max_iterations: maxIterations } : {},
    admin: true,
  });
const getRunSnapshot = (id, fromSeq = 0) => req(`/runs/${id}?from_seq=${fromSeq}`);
const cancelRun = (id) => req(`/runs/${id}`, { method: "DELETE", admin: true });
const getForecast = (id) => req(`/forecasts/${id}`);

/**
 * Open the SSE stream for a run.
 *
 * Resumption is the whole contract. `?from_seq` covers a fresh page load; after that
 * the browser's own reconnect sends `Last-Event-ID`, which the server honours — so a
 * closed laptop lid produces a delay, never a timeline with a hole in it.
 *
 * @param {string} runId
 * @param {(ev: object) => void} onEvent   one decoded RunEvent
 * @param {(reason: string) => void} onEnd fired once, on `end` or a fatal error
 * @param {number} fromSeq
 * @returns {() => void} detach
 */
function openRunStream(runId, onEvent, onEnd, fromSeq = 0) {
  const url = `${API}/runs/${runId}/stream?from_seq=${fromSeq}`;
  const es = new EventSource(url);
  let done = false;

  const finish = (reason) => {
    if (done) return;
    done = true;
    es.close();
    onEnd(reason);
  };

  es.addEventListener("run", (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    onEvent(ev);
    if (ev.type === "end") finish(ev.payload && ev.payload.status);
  });

  // EventSource retries on its own, so an error is only fatal once the socket is
  // closed for good. Anything else is a reconnect in progress and resolves itself.
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) finish("disconnected"); };

  return () => finish("detached");
}
