import { makeEquity, makePoint, makeSkeptic, makeWinRate } from "../test/fixtures";
import {
  equityLines,
  formatWinRate,
  hasAnyPoints,
  isLowSample,
  skepticRows,
  sortByKey,
  winRateRows,
} from "./analytics";

describe("formatWinRate / isLowSample", () => {
  it("formats a rate to one decimal and shows a dash for null", () => {
    expect(formatWinRate(0.625)).toBe("62.5%");
    expect(formatWinRate(0)).toBe("0.0%");
    expect(formatWinRate(1)).toBe("100.0%");
    expect(formatWinRate(null)).toBe("—");
    expect(formatWinRate(Number.NaN)).toBe("—");
  });

  it("flags fewer than 5 decisive outcomes as a low sample", () => {
    expect(isLowSample(4)).toBe(true);
    expect(isLowSample(5)).toBe(false);
    expect(isLowSample(0)).toBe(true);
  });
});

describe("winRateRows", () => {
  it("sorts by n descending and flags low samples", () => {
    const rows = winRateRows(makeWinRate().buckets);
    expect(rows.map((r) => r.key)).toEqual(["htf-bias", "liquidity-sweep", "unresolved-tag"]);
    expect(rows.map((r) => r.lowSample)).toEqual([false, true, true]);
    expect(rows[0]).toMatchObject({ pct: 0.625, n: 10, decisive: 8, wins: 5, losses: 3 });
    expect(rows[2]?.pct).toBeNull();
  });

  it("labels an empty key as (none)", () => {
    const rows = winRateRows([{ key: "", wins: 0, losses: 0, decisive: 0, win_rate: null, n: 1 }]);
    expect(rows[0]?.label).toBe("(none)");
  });

  it("sortByKey orders weeks ascending", () => {
    const rows = winRateRows(
      ["2026-W42", "2026-W40", "2026-W41"].map((key) => ({
        key,
        wins: 0,
        losses: 0,
        decisive: 0,
        win_rate: null,
        n: 1,
      })),
    );
    expect(sortByKey(rows).map((r) => r.key)).toEqual(["2026-W40", "2026-W41", "2026-W42"]);
  });
});

describe("skepticRows", () => {
  it("lists the three buckets with exact labels", () => {
    const rows = skepticRows(makeSkeptic());
    expect(rows.map((r) => r.id)).toEqual(["respected", "overruled_with_caveat", "overruled"]);
    expect(rows.map((r) => r.label)).toEqual([
      "Judge respected the objection (veto)",
      "Overruled with a caveat",
      "Overruled",
    ]);
    expect(rows[0]).toMatchObject({ n: 6, wins: 3, losses: 2, pct: 0.6 });
    expect(rows[2]?.pct).toBeNull();
  });

  it("explains that respected outcomes are counterfactual replays", () => {
    expect(skepticRows(makeSkeptic())[0]?.note.toLowerCase()).toContain("counterfactual");
  });
});

describe("equityLines", () => {
  it("returns the three lines in order with counterfactual flags and final R", () => {
    const lines = equityLines(makeEquity());
    expect(lines.map((l) => l.id)).toEqual(["published", "vetoed", "gate_rejected"]);
    expect(lines.map((l) => l.counterfactual)).toEqual([false, true, true]);
    expect(lines.map((l) => l.finalR)).toEqual([5, 2, -1]);
  });

  it("converts timestamps to epoch ms and keeps the order", () => {
    const published = equityLines(makeEquity())[0];
    expect(published?.points[0]).toEqual({ t: Date.parse("2026-10-01T08:03:00Z"), cumulativeR: 3 });
    expect(published?.points).toHaveLength(3);
  });

  it("an empty curve has a null final R while others still work", () => {
    const lines = equityLines(makeEquity({ published: [], vetoed: [makePoint("2026-10-02T08:03:00Z", 3, 3)] }));
    expect(lines[0]?.finalR).toBeNull();
    expect(lines[1]?.finalR).toBe(3);
  });
});

describe("hasAnyPoints", () => {
  it("is false only when every curve is empty", () => {
    expect(hasAnyPoints(makeEquity())).toBe(true);
    expect(hasAnyPoints(makeEquity({ published: [], vetoed: [], gate_rejected: [] }))).toBe(false);
    expect(hasAnyPoints(makeEquity({ published: [], vetoed: [], gate_rejected: [makePoint("2026-10-03T08:03:00Z", -1, -1)] }))).toBe(true);
  });
});
