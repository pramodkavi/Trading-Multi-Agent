import { useEffect, useRef, type ReactNode } from "react";
import { useAuth } from "react-oidc-context";
import { ErrorPage, FullPageMessage, buttonClass } from "../components/ErrorPage";

function returnState(): { state: { returnTo: string } } {
  const { pathname, search } = window.location;
  return { state: { returnTo: pathname === "/callback" ? "/" : `${pathname}${search}` } };
}

/**
 * Renders `children` only for a signed-in user.
 *
 * - A refresh-token stub from {@link SplitTokenStore} (expired user with a
 *   `refresh_token`) → `signinSilent()` (refresh-token grant, no Hosted UI);
 *   if that fails → `signinRedirect()`.
 * - No session → `signinRedirect()` to the Cognito Hosted UI (MFA there).
 * - A failed sign-in callback → an error page with a manual retry (no loop).
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const started = useRef(false);

  const hasRefreshStub = Boolean(auth.user?.refresh_token) && !auth.isAuthenticated;
  const shouldStart =
    !auth.isLoading && !auth.isAuthenticated && !auth.error && !auth.activeNavigator;

  useEffect(() => {
    if (auth.isAuthenticated) {
      started.current = false; // a later sign-out/expiry may start again
      return;
    }
    if (!shouldStart || started.current) return;
    started.current = true;
    const redirect = () => auth.signinRedirect(returnState()).catch(() => undefined);
    if (hasRefreshStub) {
      auth
        .signinSilent()
        .then((user) => {
          if (!user) return redirect();
          return undefined;
        })
        .catch(() => redirect());
    } else {
      void redirect();
    }
  }, [auth, shouldStart, hasRefreshStub]);

  if (auth.isAuthenticated) return <>{children}</>;

  if (auth.error && !auth.activeNavigator) {
    return (
      <ErrorPage title="Sign-in failed" message={auth.error.message}>
        <button
          type="button"
          className={buttonClass}
          onClick={() => {
            void auth.signinRedirect(returnState()).catch(() => undefined);
          }}
        >
          Sign in again
        </button>
      </ErrorPage>
    );
  }

  if (auth.isLoading) return <FullPageMessage message="Loading your session…" />;
  if (hasRefreshStub) return <FullPageMessage message="Resuming your session…" />;
  return (
    <FullPageMessage message="Redirecting to sign-in…">
      <button
        type="button"
        className={buttonClass}
        onClick={() => {
          void auth.signinRedirect(returnState()).catch(() => undefined);
        }}
      >
        Sign in
      </button>
    </FullPageMessage>
  );
}
