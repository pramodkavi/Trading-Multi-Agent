/**
 * Runtime configuration, fetched once at boot from `/config.json` (written by
 * the CDK `DashboardStack` next to the build — never baked into the bundle).
 *
 * In `vite dev`, `/config.local.json` (gitignored, in `public/`) is tried first
 * so a developer can point the dev server at the live user pool.
 */

export interface DashboardConfig {
  region: string;
  userPoolId: string;
  clientId: string;
  /** Cognito Hosted UI base URL, `https://…amazoncognito.com` (no trailing slash). */
  cognitoDomain: string;
  /** API prefix, `/api` (same origin through CloudFront) — no trailing slash. */
  apiBase: string;
  redirectUri: string;
  logoutUri: string;
  version: string;
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

const FIELDS = [
  "region",
  "userPoolId",
  "clientId",
  "cognitoDomain",
  "apiBase",
  "redirectUri",
  "logoutUri",
  "version",
] as const satisfies readonly (keyof DashboardConfig)[];

const REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d+$/;

function isHttpsUrl(value: string): boolean {
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

/** Same-origin path: `/api`, never `//host` or `/\host` (protocol-relative). */
const API_BASE_RE = /^\/[^/\\]/;

export interface ValidateOptions {
  /** The page origin the redirect/logout URIs must match (default: `window.location.origin`). */
  origin: string;
  /** `vite dev`: additionally accept any `http://localhost:*` / `http://127.0.0.1:*` origin. */
  dev: boolean;
}

function defaultOptions(): ValidateOptions {
  return { origin: window.location.origin, dev: import.meta.env.DEV };
}

function isAppUrl(value: string, opts: ValidateOptions): boolean {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.origin === opts.origin) return true;
  return (
    opts.dev &&
    url.protocol === "http:" &&
    (url.hostname === "localhost" || url.hostname === "127.0.0.1")
  );
}

function stripTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

export function validateConfig(
  raw: unknown,
  opts: ValidateOptions = defaultOptions(),
): DashboardConfig {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new ConfigError("config.json must be a JSON object.");
  }
  const record = raw as Record<string, unknown>;
  const out: Partial<Record<keyof DashboardConfig, string>> = {};
  for (const field of FIELDS) {
    const value = record[field];
    if (typeof value !== "string" || value.trim() === "") {
      throw new ConfigError(`config.json is missing field: ${field} (non-empty string required).`);
    }
    out[field] = value.trim();
  }
  const config = out as DashboardConfig;

  if (!REGION_RE.test(config.region)) {
    throw new ConfigError(`config.json field region is not an AWS region: ${config.region}`);
  }
  if (!config.userPoolId.startsWith(`${config.region}_`)) {
    throw new ConfigError(
      `config.json field userPoolId (${config.userPoolId}) does not belong to region ${config.region}.`,
    );
  }
  if (!isHttpsUrl(config.cognitoDomain)) {
    throw new ConfigError("config.json field cognitoDomain must be an https:// URL.");
  }
  config.cognitoDomain = stripTrailingSlash(config.cognitoDomain);
  if (!API_BASE_RE.test(config.apiBase)) {
    throw new ConfigError("config.json field apiBase must be a same-origin path such as /api.");
  }
  config.apiBase = stripTrailingSlash(config.apiBase);
  for (const field of ["redirectUri", "logoutUri"] as const) {
    if (!isAppUrl(config[field], opts)) {
      throw new ConfigError(
        `config.json field ${field} (${config[field]}) must be an absolute URL on this site's origin (${opts.origin}).`,
      );
    }
  }
  return config;
}

async function fetchJson(fetchFn: typeof fetch, url: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchFn(url, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch (err) {
    throw new ConfigError(`Could not fetch ${url}: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!response.ok) {
    throw new ConfigError(`Could not fetch ${url}: HTTP ${response.status}.`);
  }
  const text = await response.text();
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ConfigError(`${url} is not valid JSON.`);
  }
}

/** Fetch and validate the runtime config. Throws {@link ConfigError}. */
export async function loadConfig(
  fetchFn: typeof fetch = fetch,
  opts: ValidateOptions = defaultOptions(),
): Promise<DashboardConfig> {
  if (import.meta.env.DEV && import.meta.env.MODE !== "test") {
    let local: unknown;
    try {
      local = await fetchJson(fetchFn, "/config.local.json");
    } catch {
      // Absent (Vite serves index.html, which is not JSON) — fall through.
      local = undefined;
    }
    // Present but wrong is an error worth seeing, not a silent fallback.
    if (local !== undefined) return validateConfig(local, opts);
  }
  return validateConfig(await fetchJson(fetchFn, "/config.json"), opts);
}
