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
    const err = new Error(detailToText(data.detail || resp.statusText));
    err.status = resp.status;
    throw err;
  }
  return data;
}

export const api = {
  config: () => req("GET", "/config"),
  listRuns: () => req("GET", "/runs"),
  getRun: (id) => req("GET", `/runs/${id}`),
  createRun: (body) => req("POST", "/runs", body),
  editRun: (id, body) => req("PATCH", `/runs/${id}`, body),
  deleteRun: (id) => req("DELETE", `/runs/${id}`),
  startRun: (id) => req("POST", `/runs/${id}/start`),
  draftQuestion: (text) => req("POST", "/questions/draft", { text }),
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
    const parts = buffer.split("\n\n");
    buffer = parts.pop();
    for (const part of parts) {
      for (const line of part.split("\n")) {
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
  }
}
