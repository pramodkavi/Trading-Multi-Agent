import { ApiError } from "./client";
import { createQueryClient, retryDelay, shouldRetry } from "./queries";

describe("query retry policy", () => {
  const resuming = new ApiError(503, "db_resuming");

  it("retries db_resuming with backoff for about 60 s, then gives up", () => {
    let total = 0;
    let attempts = 0;
    while (shouldRetry(attempts, resuming)) {
      total += retryDelay(attempts, resuming);
      attempts += 1;
    }
    expect(attempts).toBe(6);
    expect(total).toBeGreaterThanOrEqual(55_000);
    expect(total).toBeLessThanOrEqual(60_000);
  });

  it("does not retry client errors", () => {
    expect(shouldRetry(0, new ApiError(401, null))).toBe(false);
    expect(shouldRetry(0, new ApiError(404, "not_found"))).toBe(false);
  });

  it("retries other failures twice", () => {
    const err = new ApiError(500, "internal");
    expect(shouldRetry(0, err)).toBe(true);
    expect(shouldRetry(1, err)).toBe(true);
    expect(shouldRetry(2, err)).toBe(false);
    expect(shouldRetry(0, new TypeError("network"))).toBe(true);
  });

  it("polls every 60 s only while visible", () => {
    const q = createQueryClient().getDefaultOptions().queries;
    expect(q?.refetchInterval).toBe(60_000);
    expect(q?.refetchIntervalInBackground).toBe(false);
    expect(q?.staleTime).toBe(30_000);
  });
});
