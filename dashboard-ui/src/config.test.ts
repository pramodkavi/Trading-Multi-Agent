import {
  ConfigError,
  loadConfig,
  validateConfig,
  type DashboardConfig,
  type ValidateOptions,
} from "./config";

const VALID: DashboardConfig = {
  region: "ap-south-1",
  userPoolId: "ap-south-1_DQCy7rINH",
  clientId: "5pbnitl2fap4ij2idqscgq808u",
  cognitoDomain: "https://crypto-signals-dash-097853039368.auth.ap-south-1.amazoncognito.com",
  apiBase: "/api",
  redirectUri: "https://d111111abcdef8.cloudfront.net/callback",
  logoutUri: "https://d111111abcdef8.cloudfront.net/",
  version: "abc1234",
};

const PROD: ValidateOptions = { origin: "https://d111111abcdef8.cloudfront.net", dev: false };

function load(fetchFn: typeof fetch, opts: ValidateOptions = PROD) {
  return loadConfig(fetchFn, opts);
}

function respond(body: string, status = 200): typeof fetch {
  return vi.fn(async () => new Response(body, { status })) as unknown as typeof fetch;
}

describe("validateConfig origin + apiBase rules", () => {
  it.each(["https://api.example.com", "//evil.example/api", "/\\evil.example", "api", "/"])(
    "rejects apiBase %s",
    (apiBase) => {
      expect(() => validateConfig({ ...VALID, apiBase }, PROD)).toThrow(/apiBase/);
    },
  );

  it.each(["redirectUri", "logoutUri"] as const)("rejects a cross-origin %s", (field) => {
    expect(() =>
      validateConfig({ ...VALID, [field]: "https://evil.example/callback" }, PROD),
    ).toThrow(new RegExp(field));
  });

  it("rejects an http://localhost redirect in a production build", () => {
    expect(() =>
      validateConfig({ ...VALID, redirectUri: "http://localhost:5173/callback" }, PROD),
    ).toThrow(/redirectUri/);
  });

  it("accepts any http://localhost:* redirect in dev mode", () => {
    const dev: ValidateOptions = { origin: "http://localhost:5173", dev: true };
    const config = validateConfig(
      {
        ...VALID,
        redirectUri: "http://localhost:5174/callback",
        logoutUri: "http://127.0.0.1:5173/",
      },
      dev,
    );
    expect(config.redirectUri).toBe("http://localhost:5174/callback");
    expect(() =>
      validateConfig({ ...VALID, redirectUri: "http://evil.example/callback" }, dev),
    ).toThrow(/redirectUri/);
  });
});

describe("loadConfig", () => {
  it("returns a valid config", async () => {
    const fetchFn = respond(JSON.stringify(VALID));
    await expect(load(fetchFn)).resolves.toEqual(VALID);
    expect(fetchFn).toHaveBeenCalledWith("/config.json", expect.objectContaining({ cache: "no-store" }));
  });

  it("strips a trailing slash from cognitoDomain and apiBase", async () => {
    const fetchFn = respond(
      JSON.stringify({ ...VALID, cognitoDomain: `${VALID.cognitoDomain}/`, apiBase: "/api/" }),
    );
    const config = await load(fetchFn);
    expect(config.cognitoDomain).toBe(VALID.cognitoDomain);
    expect(config.apiBase).toBe("/api");
  });

  it.each(Object.keys(VALID))("rejects a config missing %s", async (field) => {
    const partial = Object.fromEntries(Object.entries(VALID).filter(([k]) => k !== field));
    await expect(load(respond(JSON.stringify(partial)))).rejects.toThrow(
      new RegExp(field),
    );
  });

  it("rejects an empty string field", async () => {
    const fetchFn = respond(JSON.stringify({ ...VALID, clientId: "  " }));
    await expect(load(fetchFn)).rejects.toBeInstanceOf(ConfigError);
  });

  it("rejects a non-https cognitoDomain", async () => {
    const fetchFn = respond(
      JSON.stringify({ ...VALID, cognitoDomain: "http://example.auth.ap-south-1.amazoncognito.com" }),
    );
    await expect(load(fetchFn)).rejects.toThrow(/cognitoDomain/);
  });

  it("rejects a userPoolId from another region", async () => {
    const fetchFn = respond(JSON.stringify({ ...VALID, userPoolId: "us-east-1_abc" }));
    await expect(load(fetchFn)).rejects.toThrow(/userPoolId/);
  });

  it("rejects a relative redirectUri", async () => {
    const fetchFn = respond(JSON.stringify({ ...VALID, redirectUri: "/callback" }));
    await expect(load(fetchFn)).rejects.toThrow(/redirectUri/);
  });

  it("rejects bad JSON", async () => {
    await expect(load(respond("<!doctype html><html></html>"))).rejects.toThrow(
      /not valid JSON/,
    );
  });

  it("rejects a non-object JSON document", async () => {
    await expect(load(respond("[1,2]"))).rejects.toBeInstanceOf(ConfigError);
  });

  it("rejects an HTTP error", async () => {
    await expect(load(respond("nope", 404))).rejects.toThrow(/HTTP 404/);
  });

  it("wraps a network failure in ConfigError", async () => {
    const fetchFn = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    await expect(load(fetchFn)).rejects.toBeInstanceOf(ConfigError);
  });
});
