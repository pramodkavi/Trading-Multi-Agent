export type PeriodDays = 7 | 30 | 90;

export const PERIODS: readonly PeriodDays[] = [7, 30, 90];

const DAY_MS = 86_400_000;

/**
 * UTC midnight `days` days before `now`, as an ISO instant (`2026-10-18T00:00:00Z`).
 * Aggregate queries send only this `since` (the API defaults `until` to now), so the
 * query key is stable for a whole UTC day instead of changing every minute.
 */
export function periodSince(days: PeriodDays, now: Date = new Date()): string {
  const midnight = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  return new Date(midnight - days * DAY_MS).toISOString().replace(".000Z", "Z");
}

/** A period from a URL value: exactly `7`, `30` or `90`, else the page's default. */
export function parsePeriod(raw: string | null, fallback: PeriodDays): PeriodDays {
  const match = PERIODS.find((d) => String(d) === raw);
  return match ?? fallback;
}
