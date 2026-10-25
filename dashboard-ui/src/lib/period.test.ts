import { PERIODS, periodSince } from "./period";

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
