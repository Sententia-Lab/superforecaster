// Fetch wrappers plus the step stream reader.
//
// The stream uses fetch + ReadableStream rather than EventSource, deliberately:
// EventSource auto-reconnects on error, and under connection-as-lifetime semantics a
// reconnect would silently re-run a step the user cancelled; it also cannot send an
// Authorization header, and the trigger is semantically a POST.

const TOKEN_KEY = "sf_admin_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function headers(extra = {}) {
  const token = getToken();
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...extra,
  };
}

export function detailToText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => `${(d.loc || []).join(".")}: ${d.msg || JSON.stringify(d)}`)
      .join("; ");
  }
  return JSON.stringify(detail);
}

async function req(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: headers(),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (resp.status === 204) return null;
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    // The static frontend is mounted at "/", so it catches every path the API routers
    // decline — and answers anything that is not a GET with 405. On an API path that
    // means the route is missing, which in practice means the server predates it.
    const message =
      resp.status === 405
        ? `${method} ${path} is not a route on this server — it is probably running ` +
          `an older build. Restart the backend.`
        : detailToText(data.detail || resp.statusText);
    const err = new Error(message);
    err.status = resp.status;
    throw err;
  }
  return data;
}

export const api = {
  config: () => req("GET", "/config"),
  // Write-only. The response is the same shape `config()` returns — where each key came
  // from, never what it is — so the caller redraws from it with no follow-up GET.
  setKeys: (body) => req("PUT", "/config/keys", body),
  listRuns: () => req("GET", "/runs"),
  getRun: (id) => req("GET", `/runs/${id}`),
  createRun: (body) => req("POST", "/runs", body),
  editRun: (id, body) => req("PATCH", `/runs/${id}`, body),
  deleteRun: (id) => req("DELETE", `/runs/${id}`),
  startRun: (id) => req("POST", `/runs/${id}/start`),
  draftQuestion: (text) => req("POST", "/questions/draft", { text }),
  // Returns the whole updated run, so the caller redraws from this response with no
  // follow-up GET. 409 when something downstream has already run; 422 on a payload the
  // models reject (weights that miss 1.00, duplicate lens names, 2 or 6 sub-questions).
  editStepPayload: (runId, stepId, payload) =>
    req("PUT", `/runs/${runId}/steps/${stepId}/payload`, payload),
};

/**
 * Execute one gated step, streaming its progress.
 *
 * Calls `onEvent(frame)` for every `data:` frame. Resolves when the stream closes.
 * Aborting `signal` disconnects, which cancels the step server-side (ADR 46).
 */
export async function streamStep(runId, stepId, { onEvent, signal, maxIterations }) {
  const qs = maxIterations ? `?max_iterations=${maxIterations}` : "";
  const resp = await fetch(`/runs/${runId}/steps/${stepId}/stream${qs}`, {
    method: "POST",
    headers: headers({ Accept: "text/event-stream" }),
    signal,
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    const err = new Error(detailToText(data.detail || resp.statusText));
    err.status = resp.status;
    throw err;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split(EVENT_BOUNDARY);
    buffer = parts.pop();
    for (const part of parts) emitFrame(part, onEvent);
  }
  // A server that closes without a trailing blank line leaves a whole event in the
  // buffer. The `run` frame is always last, so dropping it is exactly the frame the
  // caller needs.
  buffer += decoder.decode();
  if (buffer.trim()) emitFrame(buffer, onEvent);
}

/**
 * SSE separates events with a blank line, and `sse_starlette` writes CRLF.
 *
 * This split used to be on `"\n\n"`, which never matches `"\r\n\r\n"` — so every frame
 * was silently discarded and the UI only ever updated from the refetch `onDone` does.
 * The cost was invisible until `useRunQueue` needed the `run` frame's payload to chain
 * one step into the next, and every queue stopped after exactly one step.
 */
const EVENT_BOUNDARY = /\r?\n\r?\n/;

function emitFrame(chunk, onEvent) {
  for (const line of chunk.split(/\r?\n/)) {
    if (!line.startsWith("data: ")) continue;
    const raw = line.slice("data: ".length).trim();
    if (!raw) continue;
    try {
      onEvent(JSON.parse(raw));
    } catch {
      // A ping or malformed frame — skip it rather than killing the stream.
    }
  }
}
