/**
 * Typed fetch wrappers for the Superforecaster API.
 *
 * Types mirror `backend/superforecaster/models.py`. Keep them in sync
 * if the backend models change. The `apiFetch` helper handles JSON
 * encoding, the admin Bearer token, and error normalization.
 */

export type Confidence = "low" | "medium" | "high";
export type QuestionStatus = "pending" | "approved" | "rejected" | "forecasted";

export interface SubPrediction {
  question: string;
  probability: number;
  rationale: string;
  confidence: Confidence;
}

export interface HistoricalAnalog {
  description: string;
  outcome: number;
  relevance: string;
}

export interface ResearchSummary {
  historical_analogs: HistoricalAnalog[];
  empirical_base_rate: number | null;
  base_rate_note: string;
  causal_forces: string[];
  evidence: { supporting: string[]; contradicting: string[] };
  uncertainties: string[];
}

export interface ForecastUpdateRecord {
  id: string;
  forecast_id: string;
  probability: number;
  confidence: Confidence;
  reasoning: string;
  is_late: boolean;
  created_at: string;
}

export interface ForecastRecord {
  id: string;
  question: string;
  resolution_criteria: string;
  resolution_source: string;
  category: string;
  submission_gap_days: number;
  submission_deadline: string;
  resolution_date: string;
  resolved_at: string | null;
  outcome: number | null;
  is_ambiguous: boolean;
  scored_probability: number | null;
  brier_score: number | null;
  last_refreshed_at: string | null;
  flagged_for_resolution_review: boolean;
  initial_reasoning: string;
  decompositions: SubPrediction[];
  research: ResearchSummary;
  updates: ForecastUpdateRecord[];
  created_at: string;
}

export interface QuestionRecord {
  id: string;
  text: string;
  resolution_criteria: string;
  proposed_resolution_date: string;
  net_score: number;
  user_vote: number | null;
  is_own: boolean;
  status: QuestionStatus;
  edited_at: string | null;
  is_deleted: boolean;
  created_at: string;
  approved_at: string | null;
  forecast_id: string | null;
}

export interface VoteResponse {
  question_id: string;
  net_score: number;
  user_vote: number | null;
}

export interface CalibrationBucket {
  range: string;
  predicted_avg: number;
  actual_frequency: number;
  count: number;
}

export interface CalibrationReport {
  aggregate_brier_score: number | null;
  total_resolved: number;
  total_ambiguous_excluded: number;
  buckets: CalibrationBucket[];
}

export interface RefreshSummary {
  total_checked: number;
  total_updated: number;
  total_skipped: number;
  total_flagged_for_review: number;
  errors: string[];
}

export interface RefreshActionResponse {
  updated: boolean;
  reason: string | null;
  update: ForecastUpdateRecord | null;
}

// ---------- core fetch helper ----------

export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

const ADMIN_TOKEN_KEY = "superforecaster_admin_token";

export function getAdminToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(ADMIN_TOKEN_KEY);
}

export function setAdminToken(token: string | null): void {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(ADMIN_TOKEN_KEY, token);
  else window.localStorage.removeItem(ADMIN_TOKEN_KEY);
}

interface FetchOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  admin?: boolean;
  query?: Record<string, string | number | undefined>;
  cache?: RequestCache;
}

export async function apiFetch<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const { method = "GET", body, admin = false, query, cache = "no-store" } = options;

  const url = new URL(`${API_BASE_URL}${path}`);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") {
        url.searchParams.set(k, String(v));
      }
    }
  }

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (admin) {
    const token = getAdminToken();
    if (!token) throw new ApiError(403, "admin token not set");
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(url.toString(), {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache,
  });

  if (res.status === 204) return undefined as T;

  let data: unknown;
  try {
    data = await res.json();
  } catch {
    data = null;
  }

  if (!res.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : `HTTP ${res.status}`;
    throw new ApiError(res.status, detail);
  }

  return data as T;
}

// ---------- forecasts ----------

export const forecasts = {
  list: (status?: string, limit = 20, offset = 0) =>
    apiFetch<ForecastRecord[]>("/forecasts", { query: { status, limit, offset } }),
  get: (id: string) => apiFetch<ForecastRecord>(`/forecasts/${id}`),
  create: (body: {
    question: string;
    resolution_criteria: string;
    resolution_source: string;
    resolution_date: string;
    category: string;
    submission_gap_days?: number;
  }) => apiFetch<ForecastRecord>("/forecasts", { method: "POST", body, admin: true }),
  resolve: (id: string, outcome: number | null) =>
    apiFetch<ForecastRecord>(`/forecasts/${id}/resolve`, {
      method: "PATCH",
      body: { outcome },
      admin: true,
    }),
  refresh: (id: string) =>
    apiFetch<RefreshActionResponse>(`/forecasts/${id}/refresh`, {
      method: "POST",
      admin: true,
    }),
};

// ---------- questions ----------

export const questions = {
  list: (params: {
    status?: QuestionStatus;
    sort?: "score" | "newest";
    limit?: number;
    offset?: number;
  } = {}) => apiFetch<QuestionRecord[]>("/questions", { query: params as Record<string, string | number | undefined> }),
  get: (id: string) => apiFetch<QuestionRecord>(`/questions/${id}`),
  topMonthly: () => apiFetch<QuestionRecord[]>("/questions/top-monthly"),
  create: (body: {
    text: string;
    resolution_criteria: string;
    proposed_resolution_date: string;
  }) => apiFetch<QuestionRecord>("/questions", { method: "POST", body }),
  edit: (id: string, body: {
    text?: string;
    resolution_criteria?: string;
    proposed_resolution_date?: string;
  }) => apiFetch<QuestionRecord>(`/questions/${id}`, { method: "PUT", body }),
  delete: (id: string) => apiFetch<void>(`/questions/${id}`, { method: "DELETE" }),
  vote: (id: string, vote: 1 | -1) =>
    apiFetch<VoteResponse>(`/questions/${id}/vote`, { method: "POST", body: { vote } }),
  unvote: (id: string) =>
    apiFetch<VoteResponse>(`/questions/${id}/vote`, { method: "DELETE" }),
  approve: (id: string, body: { resolution_date?: string; resolution_criteria?: string } = {}) =>
    apiFetch<QuestionRecord>(`/questions/${id}/approve`, { method: "POST", body, admin: true }),
  reject: (id: string) =>
    apiFetch<QuestionRecord>(`/questions/${id}/reject`, { method: "POST", admin: true }),
  forecast: (id: string) =>
    apiFetch<QuestionRecord>(`/questions/${id}/forecast`, { method: "POST", admin: true }),
};

// ---------- calibration ----------

export const calibration = {
  get: () => apiFetch<CalibrationReport>("/calibration"),
};

// ---------- admin ----------

export const admin = {
  digestPreview: () => apiFetch<QuestionRecord[]>("/admin/digest/preview", { admin: true }),
  digestRun: () =>
    apiFetch<QuestionRecord[]>("/admin/digest/run", { method: "POST", admin: true }),
  refreshRun: () =>
    apiFetch<RefreshSummary>("/admin/refresh/run", { method: "POST", admin: true }),
  refreshStatus: () =>
    apiFetch<{
      last_run_started_at: string | null;
      last_summary: RefreshSummary | null;
    }>("/admin/refresh/status", { admin: true }),
};
