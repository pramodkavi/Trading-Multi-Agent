import { CREDIT_ERROR, makeOutcome, makeOverview, makeWindow } from "../test/fixtures";
import { collectProblems, isCreditError, windowState } from "./problems";

function failedOverview(error: string | null) {
  const failed = makeOutcome({ status: "FAILED", error });
  return makeOverview({
    health: { ...makeOverview().health, last_scan: failed },
    windows_today: [makeWindow("LONDON", "2026-10-25T08:03:00Z", failed)],
  });
}

describe("isCreditError", () => {
  it("matches the Anthropic credit-balance message case-insensitively", () => {
    expect(isCreditError(CREDIT_ERROR)).toBe(true);
    expect(isCreditError("your CREDIT BALANCE is too low")).toBe(true);
  });

  it("rejects other errors and empty values", () => {
    expect(isCreditError("ReadTimeout")).toBe(false);
    expect(isCreditError("")).toBe(false);
    expect(isCreditError(null)).toBe(false);
    expect(isCreditError(undefined)).toBe(false);
  });
});

describe("collectProblems", () => {
  it("returns nothing for a healthy overview", () => {
    expect(collectProblems(makeOverview())).toEqual([]);
  });

  it("reports credits (and only credits) when the failed scan error says so", () => {
    const problems = collectProblems(failedOverview(CREDIT_ERROR));
    expect(problems.map((p) => p.id)).toEqual(["credits"]);
    expect(problems[0]?.tone).toBe("bad");
    expect(problems[0]?.title).toBe("Anthropic credit balance too low");
  });

  it("reports generic failed scans for a non-credit failure", () => {
    const problems = collectProblems(failedOverview("ReadTimeout"));
    expect(problems.map((p) => p.id)).toEqual(["scans_failed"]);
    expect(problems[0]?.title).toBe("1 scan window failed today");
  });

  it("counts a failed scan with no stored error as a generic failure", () => {
    expect(collectProblems(failedOverview(null)).map((p) => p.id)).toEqual(["scans_failed"]);
  });

  it("reports an alarm in ALARM and lists its name", () => {
    const base = makeOverview();
    const o = makeOverview({
      health: {
        ...base.health,
        alarms: [{ name: "ForecasterErrorAlarm", state: "ALARM", updated_at: "x", reason: null }],
      },
    });
    const problems = collectProblems(o);
    expect(problems.map((p) => p.id)).toEqual(["alarm"]);
    expect(problems[0]?.detail).toContain("ForecasterErrorAlarm");
  });

  it("warns when alarm status is unavailable, never treating null as healthy", () => {
    const base = makeOverview();
    const o = makeOverview({
      health: { ...base.health, alarms: null, alarms_note: "cloudwatch unavailable" },
    });
    const problems = collectProblems(o);
    expect(problems.map((p) => p.id)).toEqual(["alarms_unavailable"]);
    expect(problems[0]?.tone).toBe("warn");
    expect(problems[0]?.detail).toBe("cloudwatch unavailable");
  });

  it("warns on stale or erroring Forecaster coverage", () => {
    const base = makeOverview();
    const o = makeOverview({
      health: { ...base.health, forecaster: { ...base.health.forecaster, stale: 2 } },
    });
    expect(collectProblems(o).map((p) => p.id)).toEqual(["forecaster"]);
  });

  it("orders credits before alarms", () => {
    const failed = failedOverview(CREDIT_ERROR);
    const o = {
      ...failed,
      health: {
        ...failed.health,
        alarms: [{ name: "A", state: "ALARM" as const, updated_at: "x", reason: null }],
      },
    };
    expect(collectProblems(o).map((p) => p.id)).toEqual(["credits", "alarm"]);
  });
});

describe("windowState", () => {
  const w = (scan: ReturnType<typeof makeOutcome> | null) =>
    makeWindow("NY", "2026-10-25T13:03:00Z", scan);

  it("is pending before the fire time plus grace", () => {
    expect(windowState(w(null), new Date("2026-10-25T13:10:00Z"))).toBe("pending");
  });

  it("is missed once fire time + 15 min has passed with no scan", () => {
    expect(windowState(w(null), new Date("2026-10-25T13:18:01Z"))).toBe("missed");
  });

  it("maps scan status to ok / failed / running", () => {
    const now = new Date("2026-10-25T14:00:00Z");
    expect(windowState(w(makeOutcome({ status: "SUCCESS" })), now)).toBe("ok");
    expect(windowState(w(makeOutcome({ status: "FAILED" })), now)).toBe("failed");
    expect(windowState(w(makeOutcome({ status: "RUNNING" })), now)).toBe("running");
  });
});
