import { QueryClientProvider, type QueryObserverOptions } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { ApiError, configureApi, resetApiForTests } from "./client";
import {
  createQueryClient,
  metricsKey,
  retryDelay,
  shouldRetry,
  useAgents,
  useAgentRun,
  useMetrics,
  useOverview,
  useRecentSignals,
  useSignal,
  useSignalsJournal,
} from "./queries";

describe("endpoint hooks", () => {
  const urls: string[] = [];

  beforeEach(() => {
    urls.length = 0;
    configureApi({
      apiBase: "/api",
      auth: {
        getUser: async () => ({ access_token: "t", expired: false }),
        signinSilent: async () => null,
        signinRedirect: async () => undefined,
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        urls.push(url);
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetApiForTests();
  });

  function wrapperFor(client = createQueryClient()) {
    return ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
  }

  it("useOverview requests /api/overview and returns the body", async () => {
    const { result } = renderHook(() => useOverview(), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(urls).toEqual(["/api/overview"]);
    expect(result.current.data).toEqual({ ok: true });
  });

  it("useRecentSignals passes the limit", async () => {
    const { result } = renderHook(() => useRecentSignals(3), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(urls).toEqual(["/api/signals?limit=3"]);
  });

  it("useAgents passes the days window", async () => {
    const { result } = renderHook(() => useAgents(14), { wrapper: wrapperFor() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(urls).toEqual(["/api/system/agents?days=14"]);
  });

  it("useMetrics never polls and stays fresh for 5 minutes", async () => {
    const client = createQueryClient();
    const { result } = renderHook(() => useMetrics(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const options = client.getQueryCache().find({ queryKey: metricsKey(7) })?.options as
      | QueryObserverOptions
      | undefined;
    expect(options?.refetchInterval).toBe(false);
    expect(options?.staleTime).toBe(300_000);
  });

  it("useOverview keeps the client's 60 s poll", async () => {
    const client = createQueryClient();
    const { result } = renderHook(() => useOverview(), { wrapper: wrapperFor(client) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.getDefaultOptions().queries?.refetchInterval).toBe(60_000);
  });
});

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

describe("journal and detail hooks", () => {
  const urls: string[] = [];

  beforeEach(() => {
    urls.length = 0;
    configureApi({
      apiBase: "/api",
      auth: {
        getUser: async () => ({ access_token: "t", expired: false }),
        signinSilent: async () => null,
        signinRedirect: async () => undefined,
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        urls.push(url);
        const body = url.startsWith("/api/signals?")
          ? { items: [], next_cursor: url.includes("cursor=") ? null : "abc" }
          : { ok: true };
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetApiForTests();
  });

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={createQueryClient()}>{children}</QueryClientProvider>
  );

  it("useSignalsJournal sends the filters in a fixed order with limit 50", async () => {
    const { result } = renderHook(() => useSignalsJournal({ kind: "VETOED", symbol: "BTCUSDT" }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(urls).toEqual(["/api/signals?symbol=BTCUSDT&kind=VETOED&limit=50"]);
  });

  it("useSignalsJournal pages by the server cursor and stops when it is null", async () => {
    const { result } = renderHook(() => useSignalsJournal({}), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasNextPage).toBe(true);
    await result.current.fetchNextPage();
    await waitFor(() => expect(result.current.hasNextPage).toBe(false));
    expect(urls[1]).toBe("/api/signals?limit=50&cursor=abc");
  });

  it("a different filter set is a different query (fresh first page)", async () => {
    const { result, rerender } = renderHook(({ f }) => useSignalsJournal(f), {
      wrapper,
      initialProps: { f: { symbol: "BTCUSDT" } as const },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    rerender({ f: { symbol: "ETHUSDT" } as never });
    await waitFor(() => expect(urls).toContain("/api/signals?symbol=ETHUSDT&limit=50"));
  });

  it("useSignal requests the lifecycle by id", async () => {
    const id = "11111111-1111-4111-8111-111111111111";
    const { result } = renderHook(() => useSignal(id), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(urls).toEqual([`/api/signals/${id}`]);
  });

  it("useAgentRun fetches only when enabled", async () => {
    const { result, rerender } = renderHook(({ on }) => useAgentRun("sig-1", "run-1", on), {
      wrapper,
      initialProps: { on: false },
    });
    await new Promise((r) => setTimeout(r, 30));
    expect(urls).toEqual([]);
    rerender({ on: true });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(urls).toEqual(["/api/signals/sig-1/runs/run-1"]);
  });
});
