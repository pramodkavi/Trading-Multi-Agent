import type { StateStore } from "oidc-client-ts";

/**
 * oidc-client-ts `userStore` that splits the session (design §3.2 / §8):
 *
 * - the full `User` (access + ID token) lives in memory only, for this page;
 * - `localStorage` holds only `{refresh_token, scope, profile: {sub}}` with
 *   `expires_at: 0`, under one key.
 *
 * After a reload the memory is empty, so `get` returns that stub — an already
 * expired user — and the app calls `signinSilent()`, which oidc-client-ts
 * serves with the refresh-token grant (no Hosted UI round-trip while the
 * 30-day Cognito refresh token is valid). The access token never touches
 * persistent storage.
 */

export const SESSION_STORAGE_KEY = "crypto-signals-dashboard.session";
/** Written at sign-out, cleared when the user starts an interactive sign-in. */
export const SIGNED_OUT_KEY = "crypto-signals-dashboard.signedOutAt";

interface PersistedStub {
  key: string;
  user: {
    refresh_token: string;
    scope?: string;
    profile: { sub: string };
    expires_at: 0;
  };
}

// Module-level so every store instance in this page shares one session.
const memory = new Map<string, string>();

function defaultStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null; // e.g. storage disabled / sandboxed iframe
  }
}

export function isSignedOut(getStorage: () => Storage | null = defaultStorage): boolean {
  try {
    return getStorage()?.getItem(SIGNED_OUT_KEY) != null;
  } catch {
    return false;
  }
}

/** Sign-out step 1: from now on `set` keeps the session in memory only. */
export function markSignedOut(getStorage: () => Storage | null = defaultStorage): void {
  try {
    getStorage()?.setItem(SIGNED_OUT_KEY, String(Date.now()));
  } catch {
    // ignore (private mode)
  }
}

/** Drop both halves of the session (memory + localStorage stub). */
export function clearSession(getStorage: () => Storage | null = defaultStorage): void {
  memory.clear();
  try {
    getStorage()?.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // ignore
  }
}

/** Interactive sign-in starting: persistence is allowed again. */
export function clearSignedOut(getStorage: () => Storage | null = defaultStorage): void {
  try {
    getStorage()?.removeItem(SIGNED_OUT_KEY);
  } catch {
    // ignore
  }
}

function isStub(value: unknown): value is PersistedStub {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  if (typeof v.key !== "string") return false;
  const user = v.user as Record<string, unknown> | null | undefined;
  if (typeof user !== "object" || user === null) return false;
  const profile = user.profile as Record<string, unknown> | null | undefined;
  return (
    typeof user.refresh_token === "string" &&
    user.refresh_token !== "" &&
    (user.scope === undefined || typeof user.scope === "string") &&
    typeof profile === "object" &&
    profile !== null &&
    typeof profile.sub === "string"
  );
}

export class SplitTokenStore implements StateStore {
  private readonly getStorage: () => Storage | null;

  constructor(getStorage: () => Storage | null = defaultStorage) {
    this.getStorage = getStorage;
  }

  /** Drop the in-memory half (tests use it to simulate a page reload). */
  static clearMemory(): void {
    memory.clear();
  }

  set(key: string, value: string): Promise<void> {
    memory.set(key, value);
    const stub = this.toStub(key, value);
    // After a sign-out (in any tab) a late silent renew must not resurrect the
    // persisted session; the marker is cleared by the next interactive sign-in.
    if (stub && !isSignedOut(this.getStorage)) {
      this.write(JSON.stringify(stub));
    } else {
      this.erase();
    }
    return Promise.resolve();
  }

  get(key: string): Promise<string | null> {
    const inMemory = memory.get(key);
    if (inMemory !== undefined) return Promise.resolve(inMemory);
    const stub = this.readStub();
    if (stub && stub.key === key) return Promise.resolve(JSON.stringify(stub.user));
    return Promise.resolve(null);
  }

  remove(key: string): Promise<string | null> {
    const inMemory = memory.get(key);
    memory.delete(key);
    const stub = this.readStub();
    if (stub?.key === key || inMemory !== undefined) this.erase();
    if (inMemory !== undefined) return Promise.resolve(inMemory);
    return Promise.resolve(stub?.key === key ? JSON.stringify(stub.user) : null);
  }

  getAllKeys(): Promise<string[]> {
    const keys = new Set(memory.keys());
    const stub = this.readStub();
    if (stub) keys.add(stub.key);
    return Promise.resolve([...keys]);
  }

  private toStub(key: string, value: string): PersistedStub | null {
    let parsed: unknown;
    try {
      parsed = JSON.parse(value);
    } catch {
      return null;
    }
    if (typeof parsed !== "object" || parsed === null) return null;
    const user = parsed as Record<string, unknown>;
    const profile = user.profile as Record<string, unknown> | null | undefined;
    if (typeof user.refresh_token !== "string" || user.refresh_token === "") return null;
    if (typeof profile !== "object" || profile === null || typeof profile.sub !== "string") {
      return null;
    }
    return {
      key,
      user: {
        refresh_token: user.refresh_token,
        ...(typeof user.scope === "string" ? { scope: user.scope } : {}),
        profile: { sub: profile.sub },
        expires_at: 0,
      },
    };
  }

  private readStub(): PersistedStub | null {
    let raw: string | null;
    try {
      raw = this.getStorage()?.getItem(SESSION_STORAGE_KEY) ?? null;
    } catch {
      return null;
    }
    if (raw === null) return null;
    try {
      const parsed: unknown = JSON.parse(raw);
      return isStub(parsed) ? parsed : null;
    } catch {
      return null; // corrupt → treat as no session
    }
  }

  private write(raw: string): void {
    try {
      this.getStorage()?.setItem(SESSION_STORAGE_KEY, raw);
    } catch {
      // Private mode / quota: the session still works for this page.
    }
  }

  private erase(): void {
    try {
      this.getStorage()?.removeItem(SESSION_STORAGE_KEY);
    } catch {
      // ignore
    }
  }
}
