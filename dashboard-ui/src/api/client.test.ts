import {
  AUTH_REDIRECT_KEY,
  AUTH_REDIRECT_WINDOW_MS,
  ApiError,
  apiFetch,
  configureApi,
  isDbResuming,
  resetApiForTests,
  type AuthClient,
} from "./client";

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function user(accessToken: string) {
  return { access_token: accessToken, expired: false };
}

function makeAuth(overrides: Partial<Record<keyof AuthClient, unknown>> = {}) {
  return {
    getUser: vi.fn(async () => user("token-1")),
    signinSilent: vi.fn(async () => user("token-2")),
    signinRedirect: vi.fn(async () => undefined),
    ...overrides,
  };
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function authHeaderOf(callIndex: number): string | null {
  const init = fetchMock.mock.calls[callIndex]?.[1] as RequestInit | undefined;
  return new Headers(init?.headers).get("Authorization");
}

describe("apiFetch", () => {
  it("prefixes apiBase and adds the bearer access token", async () => {
    const auth = makeAuth();
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(jsonResponse({ db: "ok", latency_ms: 12 }));

    await expect(apiFetch("/system/db")).resolves.toEqual({ db: "ok", latency_ms: 12 });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/system/db");
    expect(authHeaderOf(0)).toBe("Bearer token-1");
  });

  it("does not send a token for unauthenticated calls", async () => {
    const auth = makeAuth();
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: "ok", version: "abc" }));

    await apiFetch("/health", { auth: false });
    expect(authHeaderOf(0)).toBeNull();
    expect(auth.getUser).not.toHaveBeenCalled();
  });

  it("401 triggers one silent renew and retries with the new token", async () => {
    const auth = makeAuth();
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ message: "Unauthorized" }, 401))
      .mockResolvedValueOnce(jsonResponse({ db: "ok", latency_ms: 3 }));

    await expect(apiFetch("/system/db")).resolves.toEqual({ db: "ok", latency_ms: 3 });
    expect(auth.signinSilent).toHaveBeenCalledTimes(1);
    expect(authHeaderOf(1)).toBe("Bearer token-2");
    expect(auth.signinRedirect).not.toHaveBeenCalled();
  });

  it("401 after a failed silent renew redirects to sign-in", async () => {
    const auth = makeAuth({
      signinSilent: vi.fn(async () => {
        throw new Error("invalid_grant");
      }),
    });
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(jsonResponse({ message: "Unauthorized" }, 401));

    const err = await apiFetch("/system/db").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect(auth.signinSilent).toHaveBeenCalledTimes(1);
    expect(auth.signinRedirect).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("401 again after a successful renew redirects (no loop)", async () => {
    const auth = makeAuth();
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ message: "Unauthorized" }, 401))
      .mockResolvedValueOnce(jsonResponse({ message: "Unauthorized" }, 401));

    await expect(apiFetch("/system/db")).rejects.toMatchObject({ status: 401 });
    expect(auth.signinSilent).toHaveBeenCalledTimes(1);
    expect(auth.signinRedirect).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("no_cross_load_401_loop: a second 401-driven redirect within 2 min is refused", async () => {
    const unauthorized = () => jsonResponse({ message: "Unauthorized" }, 401);

    // Page load 1: token rejected → silent ok → rejected again → redirect.
    let auth = makeAuth();
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(unauthorized()).mockResolvedValueOnce(unauthorized());
    await expect(apiFetch("/system/db")).rejects.toMatchObject({ status: 401 });
    expect(auth.signinRedirect).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(AUTH_REDIRECT_KEY)).not.toBeNull();

    // Page load 2 (after the Hosted UI signed in instantly): same failure → no redirect.
    resetApiForTests();
    auth = makeAuth();
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(unauthorized()).mockResolvedValueOnce(unauthorized());
    await expect(apiFetch("/system/db")).rejects.toMatchObject({
      status: 401,
      reason: "unauthorized_after_signin",
    });
    expect(auth.signinRedirect).not.toHaveBeenCalled();
  });

  it("allows a 401-driven redirect again after the window, and clears the marker on success", async () => {
    sessionStorage.setItem(AUTH_REDIRECT_KEY, String(Date.now() - AUTH_REDIRECT_WINDOW_MS - 1));
    let auth = makeAuth();
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({}, 401))
      .mockResolvedValueOnce(jsonResponse({}, 401));
    await expect(apiFetch("/x")).rejects.toMatchObject({ status: 401 });
    expect(auth.signinRedirect).toHaveBeenCalledTimes(1);

    auth = makeAuth();
    configureApi({ apiBase: "/api", auth: auth as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(jsonResponse({ db: "ok", latency_ms: 1 }));
    await apiFetch("/system/db");
    expect(sessionStorage.getItem(AUTH_REDIRECT_KEY)).toBeNull();
  });

  it("503 db_resuming surfaces as a typed ApiError", async () => {
    configureApi({ apiBase: "/api", auth: makeAuth() as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ reason: "db_resuming" }, 503, { "Retry-After": "10" }),
    );

    const err = await apiFetch("/overview").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 503, reason: "db_resuming", retryAfterSeconds: 10 });
    expect(isDbResuming(err)).toBe(true);
    expect(isDbResuming(new ApiError(503, "other"))).toBe(false);
  });

  it("404 surfaces as an ApiError (not swallowed, not HTML)", async () => {
    configureApi({ apiBase: "/api", auth: makeAuth() as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(jsonResponse({ reason: "signal_not_found" }, 404));

    const err = await apiFetch("/signals/nope").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 404, reason: "signal_not_found" });
  });

  it("an error with a non-JSON body still becomes an ApiError", async () => {
    configureApi({ apiBase: "/api", auth: makeAuth() as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(new Response("<html>Forbidden</html>", { status: 403 }));

    const err = await apiFetch("/x").catch((e: unknown) => e);
    expect(err).toMatchObject({ status: 403, reason: null });
    expect((err as ApiError).message).toMatch(/403/);
  });

  it("a 200 with a non-JSON body (e.g. index.html) is an error", async () => {
    configureApi({ apiBase: "/api", auth: makeAuth() as unknown as AuthClient });
    fetchMock.mockResolvedValueOnce(
      new Response("<!doctype html>", { status: 200, headers: { "Content-Type": "text/html" } }),
    );
    await expect(apiFetch("/x")).rejects.toBeInstanceOf(ApiError);
  });

  it("throws if used before configureApi", async () => {
    resetApiForTests();
    await expect(apiFetch("/x")).rejects.toThrow(/not configured/);
  });
});
