import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { configureApi, resetApiForTests } from "../api/client";

export interface TestClientOptions {
  retry?: number;
  retryDelayMs?: number;
  refetchIntervalMs?: number | false;
}

/** A fresh query client: no retries and no polling unless a test opts in. */
export function createTestClient(opts: TestClientOptions = {}): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: opts.retry ?? false,
        retryDelay: opts.retryDelayMs ?? 1,
        refetchInterval: opts.refetchIntervalMs ?? false,
        staleTime: 0,
      },
    },
  });
}

export function renderWithProviders(
  ui: ReactElement,
  opts: { route?: string; client?: QueryClient } = {},
): RenderResult {
  const client = opts.client ?? createTestClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[opts.route ?? "/"]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

type Route = unknown | (() => Response);

/**
 * Stub `fetch` for `/api/*`. Keys are paths after `/api` without the query
 * (e.g. `"/system/scans"`); a function value returns a custom `Response`, any
 * other value is served as a 200 JSON body. Unknown paths answer 404 `not_found`.
 * Returns the list of requested URLs (including query strings).
 */
export function stubApi(routes: Record<string, Route>): string[] {
  const requested: string[] = [];
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
      requested.push(url);
      const path = url.replace(/^\/api/, "").split("?")[0] ?? "";
      if (!(path in routes)) return jsonResponse({ reason: "not_found" }, 404);
      const value = routes[path];
      return typeof value === "function" ? (value as () => Response)() : jsonResponse(value);
    }),
  );
  return requested;
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function resetStubApi(): void {
  vi.unstubAllGlobals();
  resetApiForTests();
}
