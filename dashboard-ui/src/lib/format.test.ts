import {
  formatAge,
  formatCountdown,
  formatDuration,
  formatR,
  formatUtcDateTime,
  formatUtcTime,
  truncate,
} from "./format";

describe("UTC time formatting", () => {
  it("formats an ISO instant as UTC wall-clock text", () => {
    expect(formatUtcTime("2026-10-25T08:03:00Z")).toBe("08:03");
    expect(formatUtcDateTime("2026-10-25T08:03:00Z")).toBe("10-25 08:03");
  });

  it("shows a dash for an unparseable instant", () => {
    expect(formatUtcTime("not a date")).toBe("—");
    expect(formatUtcDateTime("")).toBe("—");
  });
});

describe("formatR", () => {
  it("signs the multiple and uses a true minus", () => {
    expect(formatR(1.25)).toBe("+1.25 R");
    expect(formatR(-0.4)).toBe("−0.40 R");
    expect(formatR(0)).toBe("0.00 R");
  });

  it("shows a dash for null and non-finite values", () => {
    expect(formatR(null)).toBe("—");
    expect(formatR(Number.NaN)).toBe("—");
  });
});

describe("formatDuration", () => {
  it("formats seconds and minutes", () => {
    expect(formatDuration(42)).toBe("42 s");
    expect(formatDuration(185)).toBe("3 m 05 s");
    expect(formatDuration(null)).toBe("—");
  });
});

describe("formatCountdown", () => {
  it("counts down in hours, minutes and seconds", () => {
    expect(formatCountdown(7500)).toBe("2 h 05 m");
    expect(formatCountdown(125)).toBe("2 m 05 s");
    expect(formatCountdown(45)).toBe("45 s");
  });

  it("says now at zero or below", () => {
    expect(formatCountdown(0)).toBe("now");
    expect(formatCountdown(-30)).toBe("now");
  });
});

describe("truncate", () => {
  it("shortens long text with an ellipsis within the max", () => {
    expect(truncate("abcdef", 4)).toBe("abc…");
    expect(truncate("abcdef", 4).length).toBe(4);
  });

  it("leaves short text alone", () => {
    expect(truncate("abc", 4)).toBe("abc");
  });
});

describe("formatAge", () => {
  it("formats an age in seconds", () => {
    expect(formatAge(30)).toBe("30 s ago");
    expect(formatAge(300)).toBe("5 min ago");
    expect(formatAge(7200)).toBe("2 h ago");
    expect(formatAge(null)).toBe("never");
  });
});
