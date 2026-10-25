import type {
  EquityCurveResponse,
  EquityPoint,
  SkepticBucket,
  SkepticValueResponse,
  WinRateBucket,
} from "../api/types";

/** A bucket with fewer decisive (WIN/LOSS) outcomes than this is flagged "low sample". */
export const LOW_SAMPLE_BELOW = 5;

export function formatWinRate(rate: number | null): string {
  return rate === null || !Number.isFinite(rate) ? "—" : `${(rate * 100).toFixed(1)}%`;
}

export function isLowSample(decisive: number): boolean {
  return decisive < LOW_SAMPLE_BELOW;
}

export interface WinRateRow {
  key: string;
  label: string;
  pct: number | null;
  n: number;
  decisive: number;
  wins: number;
  losses: number;
  lowSample: boolean;
}

/** Buckets as table rows, busiest first (ties by key). */
export function winRateRows(buckets: WinRateBucket[]): WinRateRow[] {
  return buckets
    .map((b) => ({
      key: b.key,
      label: b.key === "" ? "(none)" : b.key,
      pct: b.win_rate,
      n: b.n,
      decisive: b.decisive,
      wins: b.wins,
      losses: b.losses,
      lowSample: isLowSample(b.decisive),
    }))
    .sort((a, b) => b.n - a.n || a.key.localeCompare(b.key));
}

/** The same rows ordered by key ascending (ISO weeks read left to right). */
export function sortByKey(rows: WinRateRow[]): WinRateRow[] {
  return [...rows].sort((a, b) => a.key.localeCompare(b.key));
}

export interface SkepticRow {
  id: "respected" | "overruled_with_caveat" | "overruled";
  label: string;
  n: number;
  wins: number;
  losses: number;
  pct: number | null;
  note: string;
}

function skepticRow(
  id: SkepticRow["id"],
  label: string,
  bucket: SkepticBucket,
  note: string,
): SkepticRow {
  return { id, label, n: bucket.n, wins: bucket.wins, losses: bucket.losses, pct: bucket.win_rate, note };
}

/** The three Skeptic-value bars: how decisions ended, by how the Judge treated the objection. */
export function skepticRows(v: SkepticValueResponse): SkepticRow[] {
  return [
    skepticRow(
      "respected",
      "Judge respected the objection (veto)",
      v.respected,
      "Counterfactual: these trades were never taken; outcomes come from replays of the vetoed setups.",
    ),
    skepticRow("overruled_with_caveat", "Overruled with a caveat", v.overruled_with_caveat, "Published with a caveat."),
    skepticRow("overruled", "Overruled", v.overruled, "Published despite the objection."),
  ];
}

export interface EquityLine {
  id: "published" | "vetoed" | "gate_rejected";
  label: string;
  counterfactual: boolean;
  points: { t: number; cumulativeR: number }[];
  finalR: number | null;
}

function line(
  id: EquityLine["id"],
  label: string,
  counterfactual: boolean,
  points: EquityPoint[],
): EquityLine {
  return {
    id,
    label,
    counterfactual,
    points: points.map((p) => ({ t: Date.parse(p.at), cumulativeR: p.cumulative_r })),
    finalR: points.length > 0 ? (points[points.length - 1]?.cumulative_r ?? null) : null,
  };
}

/** The three hypothetical R curves; vetoed and gate-rejected are counterfactual replays. */
export function equityLines(c: EquityCurveResponse): EquityLine[] {
  return [
    line("published", "Published", false, c.published),
    line("vetoed", "Vetoed (counterfactual)", true, c.vetoed),
    line("gate_rejected", "Gate-rejected (counterfactual)", true, c.gate_rejected),
  ];
}

export function hasAnyPoints(c: EquityCurveResponse): boolean {
  return c.published.length + c.vetoed.length + c.gate_rejected.length > 0;
}

const DAY_MS = 86_400_000;

/**
 * One row per timestamp, one column per line, for a single LineChart. Every curve starts at 0
 * one day before the first point: an equity curve begins at zero, and a curve with a single
 * point would otherwise draw nothing. Empty when no line has a point.
 */
export function equityRows(lines: EquityLine[]): Record<string, number>[] {
  const byTime = new Map<number, Record<string, number>>();
  for (const line of lines) {
    for (const p of line.points) {
      const row = byTime.get(p.t) ?? { t: p.t };
      row[line.id] = p.cumulativeR;
      byTime.set(p.t, row);
    }
  }
  if (byTime.size === 0) return [];
  const origin: Record<string, number> = { t: Math.min(...byTime.keys()) - DAY_MS };
  for (const line of lines) origin[line.id] = 0;
  return [origin, ...[...byTime.values()].sort((a, b) => (a.t ?? 0) - (b.t ?? 0))];
}
