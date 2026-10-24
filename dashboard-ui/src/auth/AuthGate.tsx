import { useEffect, useRef, useState, type ReactNode } from "react";
import { useAuth } from "react-oidc-context";
import { ErrorPage, FullPageMessage, buttonClass } from "../components/ErrorPage";
import {
  SESSION_STORAGE_KEY,
  SIGNED_OUT_KEY,
  clearSignedOut,
  isSignedOut,
} from "./SplitTokenStore";

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
 * - Signed out (marker set by sign-out in this or another tab, or the stub
 *   removed by another tab) → a "signed out" page; no automatic redirect, so a
 *   live Hosted UI cookie cannot sign the user straight back in.
 * - A failed sign-in callback → an error page with a manual retry (no loop).
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const started = useRef(false);
  const [signedOutElsewhere, setSignedOut] = useState(false);
  // Read on every render: this tab's own sign-out sets the marker before it
  // removes the user, so the gate never races it with an automatic redirect.
  const signedOut = signedOutElsewhere || isSignedOut();

  // Cross-tab sign-out: another tab removed the persisted session.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      const sessionGone =
        (event.key === SESSION_STORAGE_KEY && event.newValue === null) ||
        (event.key === SIGNED_OUT_KEY && event.newValue !== null) ||
        event.key === null; // localStorage.clear()
      if (!sessionGone) return;
      setSignedOut(true);
      void auth.removeUser().catch(() => undefined);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [auth]);

  const signInInteractively = () => {
    clearSignedOut();
    setSignedOut(false);
    void auth.signinRedirect(returnState()).catch(() => undefined);
  };

  const hasRefreshStub = Boolean(auth.user?.refresh_token) && !auth.isAuthenticated;
  const shouldStart =
    !signedOut && !auth.isLoading && !auth.isAuthenticated && !auth.error && !auth.activeNavigator;

  useEffect(() => {
    if (auth.isAuthenticated) {
      started.current = false; // a later expiry may start again
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

  if (signedOut) {
    return (
      <FullPageMessage message="You are signed out.">
        <button type="button" className={buttonClass} onClick={signInInteractively}>
          Sign in
        </button>
      </FullPageMessage>
    );
  }

  if (auth.isAuthenticated) return <>{children}</>;

  if (auth.error && !auth.activeNavigator) {
    return (
      <ErrorPage title="Sign-in failed" message={auth.error.message}>
        <button type="button" className={buttonClass} onClick={signInInteractively}>
          Sign in again
        </button>
      </ErrorPage>
    );
  }

  if (auth.isLoading) return <FullPageMessage message="Loading your session…" />;
  if (hasRefreshStub) return <FullPageMessage message="Resuming your session…" />;
  return (
    <FullPageMessage message="Redirecting to sign-in…">
      <button type="button" className={buttonClass} onClick={signInInteractively}>
        Sign in
      </button>
    </FullPageMessage>
  );
}
