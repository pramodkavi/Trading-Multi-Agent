import { render, screen } from "@testing-library/react";
import { ApiError } from "../api/client";
import { deriveApiStatus, formatAgo, type DbState, type HealthState } from "../lib/apiStatus";
import { StatusChip } from "./StatusChip";

const HEALTH_OK: HealthState = {
  data: { status: "ok", version: "0123456789abcdef" },
  error: null,
  failureReason: null,
  isPending: false,
};
const DB_OK: DbState = {
  data: { db: "ok", latency_ms: 14 },
  error: null,
  failureReason: null,
  isPending: false,
};

describe("StatusChip", () => {
  it("shows ok with the short API version", () => {
    render(<StatusChip health={HEALTH_OK} db={DB_OK} />);
    const chip = screen.getByRole("status");
    expect(chip).toHaveTextContent(/API ok/i);
    expect(chip).toHaveTextContent("v0123456");
    expect(chip).toHaveAttribute("data-status", "ok");
  });

  it("shows waking while the database resumes (503 retrying)", () => {
    const db: DbState = {
      data: undefined,
      error: null,
      failureReason: new ApiError(503, "db_resuming"),
      isPending: true,
    };
    render(<StatusChip health={HEALTH_OK} db={db} />);
    expect(screen.getByRole("status")).toHaveTextContent(/waking the database/i);
    expect(screen.getByRole("status")).toHaveAttribute("data-status", "waking");
  });

  it("shows waking when the probe reports resuming", () => {
    const db: DbState = { ...DB_OK, data: { db: "resuming", latency_ms: 0 } };
    expect(deriveApiStatus(HEALTH_OK, db)).toBe("waking");
  });

  it("shows error when the probe fails", () => {
    const db: DbState = {
      data: undefined,
      error: new ApiError(500, "internal"),
      failureReason: new ApiError(500, "internal"),
      isPending: false,
    };
    render(<StatusChip health={HEALTH_OK} db={db} />);
    expect(screen.getByRole("status")).toHaveTextContent(/API error/i);
    expect(screen.getByRole("status")).toHaveAttribute("data-status", "error");
  });

  it("shows error when health fails or the probe says error", () => {
    const health: HealthState = { ...HEALTH_OK, data: undefined, error: new TypeError("offline") };
    expect(deriveApiStatus(health, DB_OK)).toBe("error");
    expect(deriveApiStatus(HEALTH_OK, { ...DB_OK, data: { db: "error", latency_ms: 0 } })).toBe(
      "error",
    );
  });

  it("shows checking while the first requests are in flight", () => {
    const pending = { data: undefined, error: null, failureReason: null, isPending: true };
    expect(deriveApiStatus(pending, pending)).toBe("checking");
  });
});

describe("formatAgo", () => {
  it.each([
    [0, 5_000, "not updated yet"],
    [1_000, 13_000, "updated 12 s ago"],
    [1_000, 1_000 + 5 * 60_000, "updated 5 min ago"],
  ])("(%d, %d) -> %s", (updatedAt, now, expected) => {
    expect(formatAgo(updatedAt, now)).toBe(expected);
  });
});
