/**
 * The one fetch helper for `/api/*` (design §3.4).
 *
 * - prefixes `config.apiBase` and sends `Authorization: Bearer <access token>`;
 * - on 401: one `signinSilent()` (refresh-token grant) and one retry; if that
 *   fails or 401s again → `signinRedirect()` (Hosted UI) and throw;
 * - every non-2xx — including API 403/404, which CloudFront passes through
 *   untouched — becomes an {@link ApiError} carrying `status` and the body's
 *   `reason` / `message`.
 */

interface AccessUser {
  access_token?: string;
  expired?: boolean;
}

/** The slice of oidc-client-ts `UserManager` that apiFetch needs. */
export interface AuthClient {
  getUser(): Promise<AccessUser | null>;
  signinSilent(): Promise<AccessUser | null>;
  signinRedirect(args?: { state?: unknown }): Promise<void>;
}

export class ApiError extends Error {
  readonly status: number;
  /** `reason` from the JSON body (app errors, e.g. `db_resuming`), else null. */
  readonly reason: string | null;
  /** Seconds from a `Retry-After` header, when present. */
  readonly retryAfterSeconds: number | null;

  constructor(
    status: number,
    reason: string | null,
    message?: string,
    retryAfterSeconds: number | null = null,
  ) {
    super(message ?? `API request failed: HTTP ${status}${reason ? ` (${reason})` : ""}`);
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export function isDbResuming(error: unknown): boolean {
  return error instanceof ApiError && error.status === 503 && error.reason === "db_resuming";
}

export interface ApiFetchOptions extends RequestInit {
  /** Send the access token (default true). `/api/health` is the only open route. */
  auth?: boolean;
}

interface ApiRuntime {
  apiBase: string;
  auth: AuthClient;
}

let runtime: ApiRuntime | null = null;
let silentInFlight: Promise<AccessUser | null> | null = null;

export function configureApi(next: ApiRuntime): void {
  runtime = { apiBase: next.apiBase.replace(/\/+$/, ""), auth: next.auth };
  silentInFlight = null;
}

export function resetApiForTests(): void {
  runtime = null;
  silentInFlight = null;
}

/** Concurrent 401s share one refresh. */
function renewOnce(auth: AuthClient): Promise<AccessUser | null> {
  silentInFlight ??= auth
    .signinSilent()
    .catch(() => null)
    .finally(() => {
      silentInFlight = null;
    });
  return silentInFlight;
}

/**
 * Cross-page-load loop guard: if the API rejects a token that was just issued
 * (e.g. CloudFront dropping `Authorization`, authorizer misconfig), a redirect
 * would sign in instantly via the Hosted UI cookie and 401 again, forever.
 * A 401-driven redirect is allowed once per {@link AUTH_REDIRECT_WINDOW_MS}.
 */
export const AUTH_REDIRECT_KEY = "crypto-signals-dashboard.authRedirectAt";
export const AUTH_REDIRECT_WINDOW_MS = 120_000;

function recentAuthRedirect(): boolean {
  try {
    const at = Number(window.sessionStorage.getItem(AUTH_REDIRECT_KEY));
    return Number.isFinite(at) && at > 0 && Date.now() - at < AUTH_REDIRECT_WINDOW_MS;
  } catch {
    return false;
  }
}

function recordAuthRedirect(): void {
  try {
    window.sessionStorage.setItem(AUTH_REDIRECT_KEY, String(Date.now()));
  } catch {
    // ignore
  }
}

function clearAuthRedirect(): void {
  try {
    window.sessionStorage.removeItem(AUTH_REDIRECT_KEY);
  } catch {
    // ignore
  }
}

function returnState(): { returnTo: string } {
  return { returnTo: `${window.location.pathname}${window.location.search}` };
}

async function toApiError(response: Response): Promise<ApiError> {
  let reason: string | null = null;
  let message: string | undefined;
  try {
    const body: unknown = await response.json();
    if (typeof body === "object" && body !== null) {
      const b = body as Record<string, unknown>;
      if (typeof b.reason === "string") reason = b.reason;
      if (typeof b.message === "string") message = `HTTP ${response.status}: ${b.message}`;
    }
  } catch {
    // Not JSON — keep the status only.
  }
  const retryAfter = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  return new ApiError(
    response.status,
    reason,
    message,
    Number.isFinite(retryAfter) ? retryAfter : null,
  );
}

export async function apiFetch<T>(path: string, opts: ApiFetchOptions = {}): Promise<T> {
  if (!runtime) throw new Error("apiFetch used before configureApi(): API client not configured.");
  const { apiBase, auth } = runtime;
  const { auth: withAuth = true, ...init } = opts;
  const url = `${apiBase}${path.startsWith("/") ? path : `/${path}`}`;

  const send = (user: AccessUser | null): Promise<Response> => {
    const headers = new Headers(init.headers);
    if (!headers.has("Accept")) headers.set("Accept", "application/json");
    if (withAuth && user?.access_token) headers.set("Authorization", `Bearer ${user.access_token}`);
    return fetch(url, { ...init, headers });
  };

  let response = await send(withAuth ? await auth.getUser() : null);

  if (withAuth && response.status === 401) {
    const renewed = await renewOnce(auth);
    if (renewed?.access_token) response = await send(renewed);
    if (response.status === 401) {
      if (recentAuthRedirect()) {
        throw new ApiError(
          401,
          "unauthorized_after_signin",
          "Signed in, but the API rejected the token (HTTP 401). Not redirecting again.",
        );
      }
      recordAuthRedirect();
      await auth.signinRedirect({ state: returnState() }).catch(() => undefined);
      throw await toApiError(response);
    }
  }

  if (!response.ok) throw await toApiError(response);
  if (withAuth) clearAuthRedirect();

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(response.status, "invalid_json", `API returned non-JSON for ${path}.`);
  }
}
