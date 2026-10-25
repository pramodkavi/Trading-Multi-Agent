import type { DecisionKind, SignalFilters, SignalOutcome } from "../api/types";

const DAY_MS = 86_400_000;

export const DECISION_KINDS: readonly DecisionKind[] = ["PUBLISHED", "VETOED", "GATE_REJECTED", "NO_SETUP"];
export const SIGNAL_OUTCOMES: readonly SignalOutcome[] = ["WIN", "LOSS", "BREAKEVEN", "INVALIDATED", "EXPIRED"];

/** `YYYY-MM-DD` that is a real calendar day (rejects `2026-02-30`, `2026-13-45`). */
function validDay(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const ms = Date.parse(`${value}T00:00:00Z`);
  return !Number.isNaN(ms) && new Date(ms).toISOString().slice(0, 10) === value;
}

/** The filters the page keeps in its URL; unknown keys, empty values and bad values are dropped. */
export function parseFilters(params: URLSearchParams): SignalFilters {
  const out: SignalFilters = {};
  const text = (key: string) => params.get(key)?.trim() || undefined;

  const since = text("since");
  if (since && validDay(since)) out.since = since;
  const until = text("until");
  if (until && validDay(until)) out.until = until;
  const symbol = text("symbol");
  if (symbol) out.symbol = symbol.toUpperCase();
  const kind = text("kind");
  if (kind && (DECISION_KINDS as readonly string[]).includes(kind)) out.kind = kind as DecisionKind;
  const outcome = text("outcome");
  if (outcome && (SIGNAL_OUTCOMES as readonly string[]).includes(outcome)) {
    out.outcome = outcome as SignalOutcome;
  }
  const tag = text("tag");
  if (tag) out.tag = tag;
  const session = text("session");
  if (session) out.session = session;
  const rule = text("violated_rule");
  if (rule) out.violated_rule = rule;
  return out;
}

/** Filters as the page URL query string (same key order as the API query). */
export function filtersToParams(filters: SignalFilters): URLSearchParams {
  const q = new URLSearchParams();
  for (const key of ["since", "until", "symbol", "kind", "outcome", "tag", "session", "violated_rule"] as const) {
    const value = filters[key];
    if (value) q.set(key, value);
  }
  return q;
}

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
