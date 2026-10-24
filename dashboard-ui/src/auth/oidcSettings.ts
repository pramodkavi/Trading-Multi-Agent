import { WebStorageStateStore, type StateStore, type UserManagerSettings } from "oidc-client-ts";
import type { DashboardConfig } from "../config";
import { navigateTo } from "../lib/navigate";
import { clearSession, markSignedOut } from "./SplitTokenStore";

export const OIDC_SCOPE = "openid email profile";

function sessionStorageOrNull(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

/**
 * oidc-client-ts settings for the Cognito user pool (code grant; PKCE is
 * automatic for `response_type: "code"`).
 *
 * - `metadataSeed` is merged over the fetched discovery document: Cognito's
 *   discovery has no `end_session_endpoint` / `revocation_endpoint`.
 * - `stateStore` is sessionStorage, so the in-flight sign-in state (PKCE
 *   verifier) is tab-scoped; oidc-client-ts v3 defaults to localStorage.
 * - Only the refresh token is revoked (Cognito's /oauth2/revoke takes refresh
 *   tokens and invalidates the access tokens issued from it).
 */
export function buildOidcSettings(config: DashboardConfig, userStore: StateStore): UserManagerSettings {
  const session = sessionStorageOrNull();
  return {
    authority: `https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}`,
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    post_logout_redirect_uri: config.logoutUri,
    response_type: "code",
    scope: OIDC_SCOPE,
    automaticSilentRenew: true,
    // Cognito has no check_session_iframe and its /userInfo adds nothing the ID token lacks.
    monitorSession: false,
    loadUserInfo: false,
    revokeTokensOnSignout: true,
    revokeTokenTypes: ["refresh_token"],
    userStore,
    ...(session ? { stateStore: new WebStorageStateStore({ store: session }) } : {}),
    metadataSeed: {
      end_session_endpoint: `${config.cognitoDomain}/logout`,
      revocation_endpoint: `${config.cognitoDomain}/oauth2/revoke`,
    },
  };
}

/**
 * Cognito's `/logout?client_id=…&logout_uri=…` (it ignores the standard
 * `id_token_hint` / `post_logout_redirect_uri`, and `logout_uri` must exactly
 * match one of the app client's sign-out URLs). Built here — not through
 * `signoutRedirect` — so the ID token never appears in a URL.
 */
export function buildLogoutUrl(config: DashboardConfig): string {
  const url = new URL(`${config.cognitoDomain}/logout`);
  url.searchParams.set("client_id", config.clientId);
  url.searchParams.set("logout_uri", config.logoutUri);
  return url.toString();
}

/** The slice of the auth context (or `UserManager`) sign-out needs. */
export interface SignOutClient {
  revokeTokens(types?: ("access_token" | "refresh_token")[]): Promise<void>;
  removeUser(): Promise<void>;
}

/**
 * Sign out: mark signed-out (so no tab re-persists the session), revoke the
 * refresh token (best effort), drop the user + storage, then go to Cognito's
 * /logout to end the Hosted UI session.
 */
export async function signOut(client: SignOutClient, config: DashboardConfig): Promise<void> {
  markSignedOut();
  try {
    await client.revokeTokens(["refresh_token"]);
  } catch {
    // Revocation is best effort; the local session is still dropped.
  }
  try {
    await client.removeUser();
  } catch {
    // ignore
  }
  clearSession();
  navigateTo(buildLogoutUrl(config));
}

/** Where to land after the sign-in callback: a same-origin path, never `/callback`. */
export function safeReturnPath(state: unknown): string {
  if (typeof state !== "object" || state === null) return "/";
  const returnTo = (state as { returnTo?: unknown }).returnTo;
  if (
    typeof returnTo !== "string" ||
    !returnTo.startsWith("/") ||
    returnTo.startsWith("//") ||
    returnTo.startsWith("/\\")
  ) {
    return "/";
  }
  if (returnTo === "/callback" || returnTo.startsWith("/callback?")) return "/";
  return returnTo;
}
