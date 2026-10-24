import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { Boot } from "./App";
import { ConfigError, loadConfig, type DashboardConfig } from "./config";

const mocks = vi.hoisted(() => ({
  auth: {} as Record<string, unknown>,
}));

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
    signoutRedirect: vi.fn(async () => undefined),
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

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = url.endsWith("/health")
        ? { status: "ok", version: "deadbeefcafe" }
        : { db: "ok", latency_ms: 9 };
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

  it.each([
    ["/", "Overview", "4.6"],
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
    expect(mocks.auth.signoutRedirect).toHaveBeenCalledTimes(1);
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
