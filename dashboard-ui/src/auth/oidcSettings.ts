import type { SignoutRedirectArgs, StateStore, UserManagerSettings } from "oidc-client-ts";
import type { DashboardConfig } from "../config";

export const OIDC_SCOPE = "openid email profile";

/**
 * oidc-client-ts settings for the Cognito user pool (code grant; PKCE is
 * automatic for `response_type: "code"`).
 *
 * Cognito's discovery document has no `end_session_endpoint`, so it is seeded
 * here (`metadataSeed` is merged under the fetched discovery document).
 */
export function buildOidcSettings(config: DashboardConfig, userStore: StateStore): UserManagerSettings {
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
    userStore,
    metadataSeed: {
      end_session_endpoint: `${config.cognitoDomain}/logout`,
    },
  };
}

/**
 * Cognito's `/logout` wants `client_id` + `logout_uri` (it ignores the
 * standard `id_token_hint` / `post_logout_redirect_uri`), and `logout_uri`
 * must exactly match one of the app client's sign-out URLs.
 *
 * `post_logout_redirect_uri: ""` suppresses oidc-client-ts's own
 * `post_logout_redirect_uri`/`state` and its fallback `client_id` (which it adds
 * only when there is no ID token — that would duplicate ours). The resulting URL
 * is `<cognitoDomain>/logout?[id_token_hint=…&]client_id=…&logout_uri=…`.
 * oidc-client-ts removes the stored user (memory + localStorage stub) before
 * it navigates.
 */
export function buildSignoutArgs(config: DashboardConfig): SignoutRedirectArgs {
  return {
    post_logout_redirect_uri: "",
    extraQueryParams: { client_id: config.clientId, logout_uri: config.logoutUri },
  };
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
