import { format, formatDistanceToNow } from "date-fns";

export function formatPercent(p: number | null | undefined, digits = 0): string {
  if (p === null || p === undefined) return "—";
  return `${(p * 100).toFixed(digits)}%`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return format(new Date(iso), "MMM d, yyyy");
  } catch {
    return iso;
  }
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return format(new Date(iso), "MMM d, yyyy 'at' h:mm a");
  } catch {
    return iso;
  }
}

export function relativeFromNow(iso: string | null | undefined): string {
  if (!iso) return "never";
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true });
  } catch {
    return iso;
  }
}

/**
 * Brier color: green if good (< 0.1), yellow medium, red poor.
 * Matches the spec — lower is better, max realistic value is ~0.25 for binary outcomes.
 */
export function brierColor(brier: number): "success" | "warning" | "error" {
  if (brier < 0.1) return "success";
  if (brier < 0.2) return "warning";
  return "error";
}

export function latestProbability(updates: { probability: number }[]): number | null {
  if (updates.length === 0) return null;
  return updates[updates.length - 1].probability;
}
