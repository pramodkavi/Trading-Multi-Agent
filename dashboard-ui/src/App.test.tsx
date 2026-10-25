import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { Boot } from "./App";
import { SESSION_STORAGE_KEY, SIGNED_OUT_KEY } from "./auth/SplitTokenStore";
import { ConfigError, loadConfig, type DashboardConfig } from "./config";
import { makeOverview } from "./test/fixtures";

const mocks = vi.hoisted(() => ({
  auth: {} as Record<string, unknown>,
}));

const nav = vi.hoisted(() => ({ navigateTo: vi.fn() }));
vi.mock("./lib/navigate", () => nav);

vi.mock("react-oidc-context", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: () => mocks.auth,
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

function baseAuth(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    isLoading: false,
    isAuthenticated: false,
    user: null,
    error: undefined,
    activeNavigator: undefined,
    signinRedirect: vi.fn(async () => undefined),
    signinSilent: vi.fn(async () => null),
    revokeTokens: vi.fn(async () => undefined),
    removeUser: vi.fn(async () => undefined),
    ...overrides,
  };
}

function signedIn(): Record<string, unknown> {
  return baseAuth({
    isAuthenticated: true,
    user: {
      access_token: "access",
      expired: false,
      profile: { sub: "sub-1", email: "operator@example.com" },
    },
  });
}

function stubApi(health: unknown = { status: "ok", version: "deadbeefcafe" }): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      let body: unknown = { db: "ok", latency_ms: 9 };
      if (url.endsWith("/health")) body = health;
      else if (url.endsWith("/overview")) body = makeOverview();
      else if (url.includes("/signals")) body = { items: [], next_cursor: null };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
}

function renderAt(path: string, load: () => Promise<DashboardConfig> = async () => CONFIG) {
  window.history.pushState({}, "", path);
  return render(<Boot load={load} />);
}

beforeEach(() => {
  stubApi();
  nav.navigateTo.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("App", () => {
  it("unauthenticated shows the sign-in redirect state and starts the Hosted UI redirect", async () => {
    mocks.auth = baseAuth();
    renderAt("/signals");
    expect(await screen.findByText(/redirecting to sign-in/i)).toBeInTheDocument();
    await waitFor(() => expect(mocks.auth.signinRedirect).toHaveBeenCalledTimes(1));
    expect(mocks.auth.signinRedirect).toHaveBeenCalledWith({ state: { returnTo: "/signals" } });
    expect(mocks.auth.signinSilent).not.toHaveBeenCalled();
  });

  it("a stored refresh-token stub resumes silently without the Hosted UI", async () => {
    mocks.auth = baseAuth({
      user: { refresh_token: "r", expired: true, profile: { sub: "sub-1" } },
      signinSilent: vi.fn(async () => ({ access_token: "new", expired: false })),
    });
    renderAt("/");
    await waitFor(() => expect(mocks.auth.signinSilent).toHaveBeenCalledTimes(1));
    expect(mocks.auth.signinRedirect).not.toHaveBeenCalled();
    expect(screen.getByText(/resuming your session/i)).toBeInTheDocument();
  });

  it("falls back to the Hosted UI when the silent resume fails", async () => {
    mocks.auth = baseAuth({
      user: { refresh_token: "r", expired: true, profile: { sub: "sub-1" } },
      signinSilent: vi.fn(async () => {
        throw new Error("invalid_grant");
      }),
    });
    renderAt("/");
    await waitFor(() => expect(mocks.auth.signinRedirect).toHaveBeenCalledTimes(1));
  });

  it("shows a sign-in error with a retry button", async () => {
    mocks.auth = baseAuth({ error: new Error("access_denied") });
    renderAt("/");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/sign-in failed/i);
    expect(alert).toHaveTextContent(/access_denied/);
    await userEvent.click(within(alert).getByRole("button", { name: /sign in again/i }));
    expect(mocks.auth.signinRedirect).toHaveBeenCalledTimes(1);
  });

  it("authenticated renders the nav, the user's email and the API status", async () => {
    mocks.auth = signedIn();
    renderAt("/");
    const nav = await screen.findByRole("navigation", { name: /main/i });
    for (const name of ["Overview", "Signals", "Funnel", "Analytics", "Markets", "Health"]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    }
    expect(screen.getByText("operator@example.com")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/API ok/i));
    expect(screen.getByRole("status")).toHaveTextContent("vdeadbee");
  });

  it("route / renders the Overview page, not the placeholder", async () => {
    mocks.auth = signedIn();
    renderAt("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument();
    expect(await screen.findByText("Open setups 1 / 3")).toBeInTheDocument();
    expect(screen.queryByText(/Coming in Step/)).not.toBeInTheDocument();
  });

  it.each([
    ["/signals", "Signals", "4.7"],
    ["/funnel", "Funnel", "4.8"],
    ["/analytics", "Analytics", "4.8"],
    ["/markets", "Markets", "4.9"],
    ["/health", "Health", "4.6"],
  ])("route %s renders the %s placeholder", async (path, title, step) => {
    mocks.auth = signedIn();
    renderAt(path);
    expect(await screen.findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`Coming in Step ${step.replace(".", "\\.")}`))).toBeInTheDocument();
  });

  it("an unknown route renders the 404 page", async () => {
    mocks.auth = signedIn();
    renderAt("/does/not/exist");
    expect(
      await screen.findByRole("heading", { level: 1, name: /page not found/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to overview/i })).toHaveAttribute("href", "/");
  });

  it("/callback after sign-in lands on the overview", async () => {
    mocks.auth = signedIn();
    renderAt("/callback");
    expect(await screen.findByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  it("sign out clears the session and redirects to Cognito logout", async () => {
    mocks.auth = signedIn();
    renderAt("/");
    await userEvent.click(await screen.findByRole("button", { name: /sign out/i }));
    await waitFor(() => expect(nav.navigateTo).toHaveBeenCalledTimes(1));
    expect(mocks.auth.revokeTokens).toHaveBeenCalledWith(["refresh_token"]);
    expect(mocks.auth.removeUser).toHaveBeenCalledTimes(1);
    const revokeOrder = (mocks.auth.revokeTokens as ReturnType<typeof vi.fn>).mock
      .invocationCallOrder[0];
    expect(revokeOrder).toBeLessThan(nav.navigateTo.mock.invocationCallOrder[0] ?? 0);
    const url = new URL(String(nav.navigateTo.mock.calls[0]?.[0]));
    expect(`${url.origin}${url.pathname}`).toBe(`${CONFIG.cognitoDomain}/logout`);
    expect(url.searchParams.has("id_token_hint")).toBe(false);
    expect(localStorage.getItem(SIGNED_OUT_KEY)).not.toBeNull();
    expect(mocks.auth.signinRedirect).not.toHaveBeenCalled();
  });

  it("after sign-out a reload shows 'signed out' instead of auto-redirecting", async () => {
    localStorage.setItem(SIGNED_OUT_KEY, String(Date.now()));
    mocks.auth = baseAuth();
    renderAt("/");
    expect(await screen.findByText(/you are signed out/i)).toBeInTheDocument();
    expect(mocks.auth.signinRedirect).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
    expect(localStorage.getItem(SIGNED_OUT_KEY)).toBeNull();
    expect(mocks.auth.signinRedirect).toHaveBeenCalledTimes(1);
  });

  it("cross-tab sign-out: the stub disappearing removes the user locally and shows signed out", async () => {
    mocks.auth = signedIn();
    renderAt("/");
    await screen.findByRole("navigation", { name: /main/i });

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: SESSION_STORAGE_KEY, oldValue: "{}", newValue: null }),
      );
    });
    expect(mocks.auth.removeUser).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/you are signed out/i)).toBeInTheDocument();
    expect(mocks.auth.signinRedirect).not.toHaveBeenCalled();
    expect(nav.navigateTo).not.toHaveBeenCalled();
  });

  it("an unrelated storage event does not sign out", async () => {
    mocks.auth = signedIn();
    renderAt("/");
    await screen.findByRole("navigation", { name: /main/i });
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "other", newValue: null }));
    });
    expect(screen.queryByText(/you are signed out/i)).not.toBeInTheDocument();
    expect(mocks.auth.removeUser).not.toHaveBeenCalled();
  });

  it("mobile menu: aria-controls resolves while closed; toggles; Escape closes", async () => {
    mocks.auth = signedIn();
    renderAt("/");
    const button = await screen.findByRole("button", { name: /open menu/i });
    const controlled = document.getElementById(button.getAttribute("aria-controls") ?? "");
    expect(controlled).not.toBeNull();
    expect(controlled).not.toBeVisible();
    expect(button).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(controlled).toBeVisible();
    expect(within(controlled as HTMLElement).getByRole("link", { name: "Signals" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(controlled).not.toBeVisible();
    expect(button).toHaveFocus();
  });

  it("a malformed health body does not crash the shell", async () => {
    stubApi({ status: "ok", version: 12345 });
    mocks.auth = signedIn();
    renderAt("/");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/API ok/i));
    expect(screen.getByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument();
  });

  it("test_config_error_page: a ConfigError renders a full-page error", async () => {
    mocks.auth = signedIn();
    renderAt("/", async () => {
      throw new ConfigError("config.json is missing field: clientId");
    });
    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByRole("heading", { name: /dashboard configuration error/i }),
    ).toBeInTheDocument();
    expect(alert).toHaveTextContent("config.json is missing field: clientId");
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("test_config_error_page: a missing /config.json (HTTP 404) renders a full-page error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("Not Found", { status: 404 })),
    );
    mocks.auth = signedIn();
    renderAt("/", () => loadConfig());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/dashboard configuration error/i);
    expect(alert).toHaveTextContent(/HTTP 404/);
  });
});
