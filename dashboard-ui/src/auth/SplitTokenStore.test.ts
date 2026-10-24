import { SESSION_STORAGE_KEY, SplitTokenStore } from "./SplitTokenStore";

const USER_KEY = "user:https://cognito-idp.ap-south-1.amazonaws.com/pool:client";

const FULL_USER = {
  id_token: "header.id-token-secret.sig",
  session_state: null,
  access_token: "header.access-token-secret.sig",
  refresh_token: "refresh-token-value",
  token_type: "Bearer",
  scope: "openid email profile",
  profile: { sub: "user-sub-1", email: "operator@example.com", "cognito:username": "op" },
  expires_at: 4_102_444_800,
};

function simulateReload(): SplitTokenStore {
  SplitTokenStore.clearMemory();
  return new SplitTokenStore();
}

describe("SplitTokenStore", () => {
  beforeEach(() => {
    SplitTokenStore.clearMemory();
    localStorage.clear();
  });

  it("persists_only_refresh_token", async () => {
    const store = new SplitTokenStore();
    await store.set(USER_KEY, JSON.stringify(FULL_USER));

    const raw = localStorage.getItem(SESSION_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(raw).not.toContain("access-token-secret");
    expect(raw).not.toContain("id-token-secret");
    expect(raw).not.toContain("operator@example.com");
    expect(localStorage.length).toBe(1);

    const persisted = JSON.parse(raw ?? "{}") as { key: string; user: Record<string, unknown> };
    expect(persisted.key).toBe(USER_KEY);
    expect(persisted.user).toEqual({
      refresh_token: "refresh-token-value",
      scope: "openid email profile",
      profile: { sub: "user-sub-1" },
      expires_at: 0,
    });

    // The full user (access + ID token) is served from memory in this page.
    await expect(store.get(USER_KEY)).resolves.toBe(JSON.stringify(FULL_USER));
  });

  it("returns_expired_stub_on_reload", async () => {
    await new SplitTokenStore().set(USER_KEY, JSON.stringify(FULL_USER));

    const store = simulateReload();
    const value = await store.get(USER_KEY);
    expect(value).not.toBeNull();
    const user = JSON.parse(value ?? "{}") as Record<string, unknown>;
    expect(user).toEqual({
      refresh_token: "refresh-token-value",
      scope: "openid email profile",
      profile: { sub: "user-sub-1" },
      expires_at: 0,
    });
    expect(user).not.toHaveProperty("access_token");
    expect(await store.getAllKeys()).toEqual([USER_KEY]);
    // A different key is not answered by the stub.
    await expect(store.get("user:other")).resolves.toBeNull();
  });

  it("remove_clears_storage", async () => {
    const store = new SplitTokenStore();
    await store.set(USER_KEY, JSON.stringify(FULL_USER));

    const removed = await store.remove(USER_KEY);
    expect(removed).toBe(JSON.stringify(FULL_USER));
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    await expect(store.get(USER_KEY)).resolves.toBeNull();
    await expect(simulateReload().get(USER_KEY)).resolves.toBeNull();
    expect(await store.getAllKeys()).toEqual([]);
  });

  it("does not persist a user without a refresh token", async () => {
    const store = new SplitTokenStore();
    await store.set(USER_KEY, JSON.stringify(FULL_USER));
    const { refresh_token: _drop, ...noRefresh } = FULL_USER;
    void _drop;
    await store.set(USER_KEY, JSON.stringify(noRefresh));
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    await expect(store.get(USER_KEY)).resolves.toBe(JSON.stringify(noRefresh));
  });

  it("survives_corrupt_localstorage", async () => {
    localStorage.setItem(SESSION_STORAGE_KEY, "{not json");
    const store = new SplitTokenStore();
    await expect(store.get(USER_KEY)).resolves.toBeNull();
    expect(await store.getAllKeys()).toEqual([]);

    localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({ key: USER_KEY, user: { scope: 1 } }));
    await expect(store.get(USER_KEY)).resolves.toBeNull();
  });

  it("survives localStorage throwing (private mode)", async () => {
    const throwing = {
      getItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
      setItem: () => {
        throw new DOMException("quota", "QuotaExceededError");
      },
      removeItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
    } as unknown as Storage;
    const store = new SplitTokenStore(() => throwing);
    await expect(store.set(USER_KEY, JSON.stringify(FULL_USER))).resolves.toBeUndefined();
    await expect(store.get(USER_KEY)).resolves.toBe(JSON.stringify(FULL_USER));
    await expect(store.remove(USER_KEY)).resolves.toBe(JSON.stringify(FULL_USER));
    expect(await store.getAllKeys()).toEqual([]);
  });

  it("keeps memory-only when the value is not JSON", async () => {
    const store = new SplitTokenStore();
    await store.set(USER_KEY, "garbage");
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    await expect(store.get(USER_KEY)).resolves.toBe("garbage");
  });
});
