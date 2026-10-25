import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { makeFunnel, makeFunnelGroup } from "../test/fixtures";
import {
  createTestClient,
  jsonResponse,
  renderWithProviders,
  resetStubApi,
  stubApi,
} from "../test/render";
import FunnelPage from "./FunnelPage";

function Where() {
  const loc = useLocation();
  return <output data-testid="where">{`${loc.pathname}${loc.search}`}</output>;
}

function renderPage(route = "/funnel", opts: Parameters<typeof renderWithProviders>[1] = {}) {
  return renderWithProviders(
    <>
      <FunnelPage />
      <Where />
    </>,
    { route, ...opts },
  );
}

const SINCE_30 = "since=2026-09-25T00%3A00%3A00Z";
const SINCE_7 = "since=2026-10-18T00%3A00%3A00Z";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-25T10:00:00Z"));
});

afterEach(() => {
  resetStubApi();
  vi.useRealTimers();
});

describe("FunnelPage", () => {
  it("shows the five stages with exact counts and the Judge split", async () => {
    stubApi({ "/funnel": makeFunnel() });
    renderPage();

    const stages = await screen.findByRole("list", { name: "Funnel stages" });
    for (const [label, count] of [
      ["Scans", "40"],
      ["Symbols scanned", "160"],
      ["Analyzer proposals", "30"],
      ["Passed the risk gate", "12"],
      ["Published", "3"],
    ] as const) {
      const row = within(stages).getByText(label).closest("li") as HTMLElement;
      expect(within(row).getByText(count)).toBeInTheDocument();
    }
    expect(screen.getByText("Judge: 2 publish · 1 with caveat · 9 skip")).toBeInTheDocument();
  });

  it("requests the 30-day default grouped by day, and re-requests on period and grouping changes", async () => {
    const requested = stubApi({ "/funnel": makeFunnel() });
    renderPage();
    await screen.findByRole("list", { name: "Funnel stages" });
    expect(requested).toEqual([`/api/funnel?${SINCE_30}&group_by=day`]);

    await userEvent.click(screen.getByRole("radio", { name: "7 days" }));
    await waitFor(() => expect(requested).toContain(`/api/funnel?${SINCE_7}&group_by=day`));
    expect(screen.getByTestId("where")).toHaveTextContent("/funnel?days=7");

    await userEvent.click(screen.getByRole("radio", { name: "Symbol" }));
    await waitFor(() => expect(requested).toContain(`/api/funnel?${SINCE_7}&group_by=symbol`));
    expect(screen.getByTestId("where")).toHaveTextContent("/funnel?days=7&group=symbol");
  });

  it("falls back to 30 days / day for bad URL values", async () => {
    const requested = stubApi({ "/funnel": makeFunnel() });
    renderPage("/funnel?days=bogus&group=nope");
    await screen.findByRole("list", { name: "Funnel stages" });
    expect(requested).toEqual([`/api/funnel?${SINCE_30}&group_by=day`]);
    expect(screen.getByRole("radio", { name: "30 days" })).toHaveAttribute("aria-checked", "true");
  });

  it("lists the top rejection rules for the last 7 and 30 days, as text", async () => {
    const hostile = "<script>alert(1)</script>";
    stubApi({
      "/funnel": makeFunnel({
        top_rejections_7d: [{ rule: hostile, count: 7 }],
        top_rejections_30d: [
          { rule: "RULE_2_MIN_RR", count: 10 },
          { rule: "RULE_3_PREMIUM_DISCOUNT", count: 8 },
        ],
      }),
    });
    const { container } = renderPage();

    const week = await screen.findByRole("table", { name: /last 7 days/i });
    expect(within(week).getByText(hostile)).toBeInTheDocument();
    expect(within(week).getByText("7")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    const month = screen.getByRole("table", { name: /last 30 days/i });
    expect(within(month).getByText("RULE_2_MIN_RR")).toBeInTheDocument();
    expect(within(month).getByText("8")).toBeInTheDocument();
  });

  it("says so when there are no rejections in a window", async () => {
    stubApi({ "/funnel": makeFunnel({ top_rejections_7d: [], top_rejections_30d: [] }) });
    renderPage();
    expect(await screen.findByText("No rejections in the last 7 days.")).toBeInTheDocument();
    expect(screen.getByText("No rejections in the last 30 days.")).toBeInTheDocument();
  });

  it("shows an empty state instead of a funnel when nothing was scanned", async () => {
    const empty = makeFunnelGroup("total", {
      scans: 0,
      symbols_scanned: 0,
      proposals: 0,
      gate_rejected: 0,
      gate_passed: 0,
      judge_publish: 0,
      judge_caveat: 0,
      judge_skip: 0,
      published: 0,
      rejections_by_rule: {},
    });
    stubApi({ "/funnel": makeFunnel({ groups: [], totals: empty, top_rejections_7d: [], top_rejections_30d: [] }) });
    const { container } = renderPage();

    expect(await screen.findByText("No scans in this period.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Funnel stages" })).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/NaN|Infinity|undefined/);
  });

  it("folds more than five rules into Other in the data table", async () => {
    const rules = Object.fromEntries(Array.from({ length: 8 }, (_, i) => [`RULE_${i + 1}`, 8 - i]));
    stubApi({ "/funnel": makeFunnel({ groups: [makeFunnelGroup("2026-10-25", { rejections_by_rule: rules })] }) });
    renderPage();
    expect(await screen.findByRole("columnheader", { name: "Other", hidden: true })).toBeInTheDocument();
  });

  it("explains a rejected date range (422) instead of failing", async () => {
    stubApi({ "/funnel": () => jsonResponse({ detail: "since must be before until" }, 422) });
    renderPage();
    expect(await screen.findByText("That date range is not valid.")).toBeInTheDocument();
  });

  it("keeps the last good data when a background refresh fails", async () => {
    let calls = 0;
    stubApi({
      "/funnel": () => (calls++ === 0 ? jsonResponse(makeFunnel()) : jsonResponse({ reason: "db_error" }, 502)),
    });
    renderPage("/funnel", { client: createTestClient({ refetchIntervalMs: 40 }) });

    await screen.findByRole("list", { name: "Funnel stages" });
    expect(await screen.findByText(/could not refresh the funnel/i)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Funnel stages" })).toBeInTheDocument();
  });

  it("shows the waking state while Aurora resumes, then the funnel", async () => {
    let calls = 0;
    stubApi({
      "/funnel": () => (calls++ === 0 ? jsonResponse({ reason: "db_resuming" }, 503) : jsonResponse(makeFunnel())),
    });
    renderPage("/funnel", { client: createTestClient({ retry: 2, retryDelayMs: 300 }) });

    expect(await screen.findByText(/waking the database/i)).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Funnel stages" })).toBeInTheDocument();
  });

  it("offers a retry when the funnel cannot load at all", async () => {
    stubApi({ "/funnel": () => jsonResponse({ reason: "db_error" }, 502) });
    renderPage();
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/could not load the funnel/i)).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
