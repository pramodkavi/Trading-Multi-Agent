import { makeFunnelGroup } from "../test/fixtures";
import { funnelStages, groupLabel, judgeBreakdown, rejectionSeries } from "./funnel";

describe("funnelStages", () => {
  it("lists the five stages in order with exact counts", () => {
    const stages = funnelStages(makeFunnelGroup("total"));
    expect(stages.map((s) => s.id)).toEqual(["scans", "symbols", "proposals", "gate_passed", "published"]);
    expect(stages.map((s) => s.count)).toEqual([40, 160, 30, 12, 3]);
  });

  it("computes ratios; scans and symbols are different units so they carry none", () => {
    const [scans, symbols, proposals, passed, published] = funnelStages(makeFunnelGroup("total"));
    expect(scans?.ofPrevious).toBeNull();
    expect(scans?.ofScanned).toBeNull();
    expect(symbols?.ofPrevious).toBeNull();
    expect(symbols?.ofScanned).toBeNull();
    expect(proposals?.ofPrevious).toBeCloseTo(30 / 160);
    expect(proposals?.ofScanned).toBeCloseTo(30 / 160);
    expect(passed?.ofPrevious).toBeCloseTo(12 / 30);
    expect(passed?.ofScanned).toBeCloseTo(12 / 160);
    expect(published?.ofPrevious).toBeCloseTo(3 / 12);
    expect(published?.ofScanned).toBeCloseTo(3 / 160);
  });

  it("never divides by zero: an empty previous stage gives null, not Infinity or NaN", () => {
    const stages = funnelStages(
      makeFunnelGroup("total", { scans: 0, symbols_scanned: 0, proposals: 0, gate_passed: 0, published: 0 }),
    );
    for (const s of stages) {
      expect(s.count).toBe(0);
      expect(s.ofPrevious).toBeNull();
      expect(s.ofScanned).toBeNull();
    }
  });

  it("a stage after an empty one has no ratio even if it is non-zero", () => {
    const stages = funnelStages(makeFunnelGroup("total", { proposals: 0, gate_passed: 2 }));
    expect(stages.find((s) => s.id === "gate_passed")?.ofPrevious).toBeNull();
  });
});

describe("judgeBreakdown", () => {
  it("passes the four Judge counts through", () => {
    expect(judgeBreakdown(makeFunnelGroup("total"))).toEqual({ publish: 2, caveat: 1, skip: 9, none: 0 });
  });
});

describe("rejectionSeries", () => {
  const rules = (n: number) =>
    Object.fromEntries(Array.from({ length: n }, (_, i) => [`RULE_${i + 1}`, n - i]));

  it("keeps the top N rules by total and sums the rest into Other", () => {
    const groups = [makeFunnelGroup("2026-10-24", { rejections_by_rule: rules(8) })];
    const series = rejectionSeries(groups, 5);
    expect(series.rules).toEqual(["RULE_1", "RULE_2", "RULE_3", "RULE_4", "RULE_5", "Other"]);
    expect(series.rows[0]).toMatchObject({ key: "2026-10-24", RULE_1: 8, RULE_5: 4, Other: 3 + 2 + 1 });
  });

  it("uses 0 for a rule absent from a group and has no Other when there are few rules", () => {
    const groups = [
      makeFunnelGroup("a", { rejections_by_rule: { X: 2 } }),
      makeFunnelGroup("b", { rejections_by_rule: { Y: 1 } }),
    ];
    const series = rejectionSeries(groups, 5);
    expect(series.rules).toEqual(["X", "Y"]);
    expect(series.rows).toEqual([
      { key: "a", X: 2, Y: 0 },
      { key: "b", X: 0, Y: 1 },
    ]);
  });

  it("breaks ties by rule name", () => {
    const groups = [makeFunnelGroup("a", { rejections_by_rule: { B: 1, A: 1 } })];
    expect(rejectionSeries(groups).rules).toEqual(["A", "B"]);
  });

  it("is empty for no groups or no rejections", () => {
    expect(rejectionSeries([])).toEqual({ rules: [], rows: [] });
    const quiet = [makeFunnelGroup("a", { rejections_by_rule: {} })];
    expect(rejectionSeries(quiet).rules).toEqual([]);
  });
});

describe("groupLabel", () => {
  it("shortens day keys and leaves the others alone", () => {
    expect(groupLabel("2026-10-25", "day")).toBe("10-25");
    expect(groupLabel("LONDON", "window")).toBe("LONDON");
    expect(groupLabel("BTCUSDT", "symbol")).toBe("BTCUSDT");
  });
});
