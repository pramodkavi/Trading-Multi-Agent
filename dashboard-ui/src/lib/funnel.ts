import type { FunnelGroup, FunnelGroupBy } from "../api/types";

export interface Stage {
  id: "scans" | "symbols" | "proposals" | "gate_passed" | "published";
  label: string;
  count: number;
  /** count / the stage before it; `null` when that stage is empty or is a different unit. */
  ofPrevious: number | null;
  /** count / symbols scanned (the per-symbol decisions the funnel starts from). */
  ofScanned: number | null;
}

/** `n / d`, or `null` when there is nothing to divide by (never `Infinity` / `NaN`). */
function ratio(n: number, d: number): number | null {
  return d > 0 ? n / d : null;
}

/**
 * The funnel's five stages. Scans (invocations) and symbols scanned (decisions) are different
 * units, so neither carries a ratio; from "proposals" on, each is compared with the stage
 * before it and with the symbols scanned.
 */
export function funnelStages(t: FunnelGroup): Stage[] {
  const symbols = t.symbols_scanned;
  return [
    { id: "scans", label: "Scans", count: t.scans, ofPrevious: null, ofScanned: null },
    { id: "symbols", label: "Symbols scanned", count: symbols, ofPrevious: null, ofScanned: null },
    {
      id: "proposals",
      label: "Analyzer proposals",
      count: t.proposals,
      ofPrevious: ratio(t.proposals, symbols),
      ofScanned: ratio(t.proposals, symbols),
    },
    {
      id: "gate_passed",
      label: "Passed the risk gate",
      count: t.gate_passed,
      ofPrevious: ratio(t.gate_passed, t.proposals),
      ofScanned: ratio(t.gate_passed, symbols),
    },
    {
      id: "published",
      label: "Published",
      count: t.published,
      ofPrevious: ratio(t.published, t.gate_passed),
      ofScanned: ratio(t.published, symbols),
    },
  ];
}

export function judgeBreakdown(t: FunnelGroup): { publish: number; caveat: number; skip: number; none: number } {
  return { publish: t.judge_publish, caveat: t.judge_caveat, skip: t.judge_skip, none: t.judge_none };
}

export interface RejectionSeries {
  rules: string[];
  rows: Record<string, string | number>[];
}

export const OTHER_RULES = "Other";

/**
 * Rejections per group for a stacked chart: the `topN` rules by total (ties by name) and, when
 * more exist, an "Other" series summing the rest. Absent rules count 0.
 */
export function rejectionSeries(groups: FunnelGroup[], topN = 5): RejectionSeries {
  const totals = new Map<string, number>();
  for (const g of groups) {
    for (const [rule, count] of Object.entries(g.rejections_by_rule)) {
      totals.set(rule, (totals.get(rule) ?? 0) + count);
    }
  }
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const top = ranked.slice(0, topN).map(([rule]) => rule);
  const hasOther = ranked.length > topN;
  const rules = hasOther ? [...top, OTHER_RULES] : top;

  const rows = groups.map((g) => {
    const row: Record<string, string | number> = { key: g.key };
    for (const rule of top) row[rule] = g.rejections_by_rule[rule] ?? 0;
    if (hasOther) {
      row[OTHER_RULES] = Object.entries(g.rejections_by_rule)
        .filter(([rule]) => !top.includes(rule))
        .reduce((sum, [, count]) => sum + count, 0);
    }
    return row;
  });
  return { rules, rows };
}

/** Axis / table label of a group key: `2026-10-25` → `10-25` for days, others unchanged. */
export function groupLabel(key: string, groupBy: FunnelGroupBy): string {
  return groupBy === "day" && /^\d{4}-\d{2}-\d{2}$/.test(key) ? key.slice(5) : key;
}
