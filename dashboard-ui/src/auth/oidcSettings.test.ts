import { WebStorageStateStore } from "oidc-client-ts";
import type { DashboardConfig } from "../config";
import { SESSION_STORAGE_KEY, SIGNED_OUT_KEY, SplitTokenStore } from "./SplitTokenStore";
import { buildLogoutUrl, buildOidcSettings, safeReturnPath, signOut } from "./oidcSettings";

const nav = vi.hoisted(() => ({ calls: [] as string[] }));
vi.mock("../lib/navigate", () => ({
  navigateTo: (url: string) => {
    nav.calls.push(`navigate:${url}`);
  },
}));

const CONFIG: DashboardConfig = {
  region: "ap-south-1",
  userPoolId: "ap-south-1_DQCy7rINH",
  clientId: "5pbnitl2fap4ij2idqscgq808u",
  cognitoDomain: "https://crypto-signals-dash-097853039368.auth.ap-south-1.amazoncognito.com",
  apiBase: "/api",
  redirectUri: "https://d111111abcdef8.cloudfront.net/callback",
  logoutUri: "https://d111111abcdef8.cloudfront.net/",
  version: "abc1234",
};

const DOMAIN = "https://crypto-signals-dash-097853039368.auth.ap-south-1.amazoncognito.com";

describe("buildOidcSettings", () => {
  it("points at the Cognito user pool issuer with code + PKCE and the split store", () => {
    const store = new SplitTokenStore();
    const s = buildOidcSettings(CONFIG, store);
    expect(s.authority).toBe("https://cognito-idp.ap-south-1.amazonaws.com/ap-south-1_DQCy7rINH");
    expect(s.client_id).toBe(CONFIG.clientId);
    expect(s.redirect_uri).toBe(CONFIG.redirectUri);
    expect(s.post_logout_redirect_uri).toBe(CONFIG.logoutUri);
    expect(s.response_type).toBe("code");
    expect(s.scope).toBe("openid email profile");
    expect(s.automaticSilentRenew).toBe(true);
    expect(s.userStore).toBe(store);
    expect(s.revokeTokensOnSignout).toBe(true);
    expect(s.revokeTokenTypes).toEqual(["refresh_token"]);
    expect(s.metadataSeed).toEqual({
      end_session_endpoint: `${DOMAIN}/logout`,
      revocation_endpoint: `${DOMAIN}/oauth2/revoke`,
    });
  });

  it("keeps the in-flight sign-in state (PKCE verifier) in sessionStorage", async () => {
    const s = buildOidcSettings(CONFIG, new SplitTokenStore());
    expect(s.stateStore).toBeInstanceOf(WebStorageStateStore);
    await s.stateStore?.set("probe", "v");
    expect(Object.keys(sessionStorage).some((k) => k.endsWith("probe"))).toBe(true);
    expect(Object.keys(localStorage).some((k) => k.endsWith("probe"))).toBe(false);
  });
});

describe("buildLogoutUrl", () => {
  it("is Cognito's /logout with exactly one client_id, logout_uri and no ID token", () => {
    const url = new URL(buildLogoutUrl(CONFIG));
    expect(`${url.origin}${url.pathname}`).toBe(`${DOMAIN}/logout`);
    expect(url.searchParams.getAll("client_id")).toEqual([CONFIG.clientId]);
    expect(url.searchParams.get("logout_uri")).toBe(CONFIG.logoutUri);
    expect([...url.searchParams.keys()].sort()).toEqual(["client_id", "logout_uri"]);
  });
});

describe("signOut", () => {
  beforeEach(() => {
    nav.calls.length = 0;
  });

  it("revokes the refresh token, removes the user and clears storage before navigating", async () => {
    localStorage.setItem(SESSION_STORAGE_KEY, "{}");
    const client = {
      revokeTokens: vi.fn(async (types?: string[]) => {
        nav.calls.push(`revoke:${(types ?? []).join(",")}`);
        // The marker is already set, so a concurrent store.set cannot re-persist.
        expect(localStorage.getItem(SIGNED_OUT_KEY)).not.toBeNull();
      }),
      removeUser: vi.fn(async () => {
        nav.calls.push("removeUser");
      }),
    };
    await signOut(client, CONFIG);
    expect(nav.calls).toEqual(["revoke:refresh_token", "removeUser", `navigate:${buildLogoutUrl(CONFIG)}`]);
    expect(localStorage.getItem(SESSION_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem(SIGNED_OUT_KEY)).not.toBeNull();
  });

  it("still signs out locally when revocation fails", async () => {
    const client = {
      revokeTokens: vi.fn(async () => {
        throw new Error("network");
      }),
      removeUser: vi.fn(async () => undefined),
    };
    await signOut(client, CONFIG);
    expect(client.removeUser).toHaveBeenCalledTimes(1);
    expect(nav.calls).toEqual([`navigate:${buildLogoutUrl(CONFIG)}`]);
  });
});

describe("safeReturnPath", () => {
  it.each([
    [{ returnTo: "/signals?x=1" }, "/signals?x=1"],
    [{ returnTo: "//evil.example" }, "/"],
    [{ returnTo: "https://evil.example/" }, "/"],
    [{ returnTo: "/\\evil.example" }, "/"],
    [{ returnTo: "/callback" }, "/"],
    [undefined, "/"],
    ["/signals", "/"],
  ])("%j -> %s", (state, expected) => {
    expect(safeReturnPath(state)).toBe(expected);
  });
});
