import { OidcClient } from "oidc-client-ts";
import type { DashboardConfig } from "../config";
import { SplitTokenStore } from "./SplitTokenStore";
import { buildOidcSettings, buildSignoutArgs, safeReturnPath } from "./oidcSettings";

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
    expect(s.metadataSeed).toEqual({
      end_session_endpoint:
        "https://crypto-signals-dash-097853039368.auth.ap-south-1.amazoncognito.com/logout",
    });
  });
});

describe("buildSignoutArgs", () => {
  it("adds Cognito's client_id + logout_uri query parameters", () => {
    expect(buildSignoutArgs(CONFIG)).toEqual({
      post_logout_redirect_uri: "",
      extraQueryParams: { client_id: CONFIG.clientId, logout_uri: CONFIG.logoutUri },
    });
  });

  it("produces Cognito's /logout URL with exactly one client_id (real oidc-client-ts)", async () => {
    const settings = buildOidcSettings(CONFIG, new SplitTokenStore());
    // No network: the seeded end_session_endpoint is used once metadata is set.
    const client = new OidcClient({
      ...settings,
      metadata: { issuer: settings.authority, ...settings.metadataSeed },
    });
    for (const idToken of [undefined, "a.b.c"]) {
      const req = await client.createSignoutRequest({
        ...buildSignoutArgs(CONFIG),
        id_token_hint: idToken,
      });
      const url = new URL(req.url);
      expect(`${url.origin}${url.pathname}`).toBe(`${CONFIG.cognitoDomain}/logout`);
      expect(url.searchParams.getAll("client_id")).toEqual([CONFIG.clientId]);
      expect(url.searchParams.get("logout_uri")).toBe(CONFIG.logoutUri);
      expect(url.searchParams.has("post_logout_redirect_uri")).toBe(false);
    }
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
