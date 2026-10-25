import type { SignalFilters } from "../api/types";

const DAY_MS = 86_400_000;

/** `2026-10-01` → `2026-10-01T00:00:00Z`; with `addDays` the exclusive end of the chosen day. */
function dayToInstant(day: string, addDays = 0): string {
  const ms = Date.parse(`${day}T00:00:00Z`) + addDays * DAY_MS;
  return new Date(ms).toISOString().replace(".000Z", "Z");
}

/**
 * The `GET /api/signals` query string: fixed key order (since, until, symbol, kind,
 * outcome, tag, session, violated_rule, limit, cursor), empty filters omitted. The
 * API's `until` is exclusive, so the operator's end day is sent as the next midnight.
 */
export function toApiQuery(filters: SignalFilters, limit: number, cursor?: string): string {
  const q = new URLSearchParams();
  if (filters.since) q.set("since", dayToInstant(filters.since));
  if (filters.until) q.set("until", dayToInstant(filters.until, 1));
  if (filters.symbol) q.set("symbol", filters.symbol);
  if (filters.kind) q.set("kind", filters.kind);
  if (filters.outcome) q.set("outcome", filters.outcome);
  if (filters.tag) q.set("tag", filters.tag);
  if (filters.session) q.set("session", filters.session);
  if (filters.violated_rule) q.set("violated_rule", filters.violated_rule);
  q.set("limit", String(limit));
  if (cursor) q.set("cursor", cursor);
  return q.toString();
}
