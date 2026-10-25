import { PERIODS, parsePeriod, periodSince } from "./period";

describe("parsePeriod", () => {
  it("accepts 7, 30 and 90 and nothing else", () => {
    expect(parsePeriod("7", 30)).toBe(7);
    expect(parsePeriod("90", 30)).toBe(90);
    expect(parsePeriod("30", 90)).toBe(30);
  });

  it("falls back for missing, unknown or malformed values", () => {
    for (const bad of [null, "", "bogus", "15", "7.5", "-7", " 7", "7 "]) {
      expect(parsePeriod(bad, 30)).toBe(30);
    }
  });
});

describe("periodSince", () => {
  it("is UTC midnight N days before now", () => {
    const now = new Date("2026-10-25T10:00:00Z");
    expect(periodSince(7, now)).toBe("2026-10-18T00:00:00Z");
    expect(periodSince(30, now)).toBe("2026-09-25T00:00:00Z");
    expect(periodSince(90, now)).toBe("2026-07-27T00:00:00Z");
  });

  it("is stable for the whole UTC day", () => {
    const start = periodSince(7, new Date("2026-10-25T00:00:00Z"));
    const end = periodSince(7, new Date("2026-10-25T23:59:59Z"));
    expect(start).toBe(end);
    expect(periodSince(7, new Date("2026-10-26T00:00:00Z"))).not.toBe(start);
  });

  it("crosses a month boundary", () => {
    expect(periodSince(7, new Date("2026-03-03T12:00:00Z"))).toBe("2026-02-24T00:00:00Z");
  });

  it("offers 7, 30 and 90 days", () => {
    expect(PERIODS).toEqual([7, 30, 90]);
  });
});
