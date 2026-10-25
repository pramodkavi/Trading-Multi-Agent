import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import type { SignalsPage as SignalsPageData } from "../api/types";
import { rowsToCsv } from "../lib/csv";
import { makeRow } from "../test/fixtures";
import {
  createTestClient,
  jsonResponse,
  renderWithProviders,
  resetStubApi,
  stubApi,
} from "../test/render";
import SignalsPage from "./SignalsPage";

const ID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function page(items: ReturnType<typeof makeRow>[], next: string | null = null): SignalsPageData {
  return { items, next_cursor: next };
}

function Where() {
  const loc = useLocation();
  return <output data-testid="where">{`${loc.pathname}${loc.search}`}</output>;
}

function renderPage(route = "/signals", opts: Parameters<typeof renderWithProviders>[1] = {}) {
  return renderWithProviders(
    <>
      <SignalsPage />
      <Where />
    </>,
    { route, ...opts },
  );
}

afterEach(() => {
  resetStubApi();
  vi.unstubAllGlobals();
});

describe("SignalsPage", () => {
  it("lists decisions with kind badges and links to each detail page", async () => {
    stubApi({
      "/signals": page([
        makeRow({ id: ID_A, symbol: "BTCUSDT", decision_kind: "PUBLISHED" }),
        makeRow({ id: ID_B, symbol: "ETHUSDT", decision_kind: "GATE_REJECTED", violated_rule: "RULE_2_MIN_RR" }),
      ]),
    });
    renderPage();

    const link = await screen.findByRole("link", { name: "BTCUSDT" });
    expect(link).toHaveAttribute("href", `/signals/${ID_A}`);
    expect(screen.getByRole("link", { name: "ETHUSDT" })).toHaveAttribute("href", `/signals/${ID_B}`);
    const table = within(screen.getByRole("table"));
    expect(table.getByText("PUBLISHED").closest("[data-tone]")).toHaveAttribute("data-tone", "ok");
    expect(table.getByText("RULE_2_MIN_RR")).toBeInTheDocument();
  });

  it("reads filters from the URL, sends them to the API and pre-fills the controls", async () => {
    const requested = stubApi({ "/signals": page([makeRow()]) });
    renderPage("/signals?symbol=ETHUSDT&kind=VETOED");

    await screen.findByRole("link", { name: "BTCUSDT" });
    expect(requested).toEqual(["/api/signals?symbol=ETHUSDT&kind=VETOED&limit=50"]);
    expect(screen.getByLabelText("Symbol")).toHaveValue("ETHUSDT");
    expect(screen.getByLabelText("Decision")).toHaveValue("VETOED");
  });

  it("writes a changed filter to the URL and fetches a fresh first page", async () => {
    const requested = stubApi({ "/signals": page([makeRow()]) });
    renderPage();
    await screen.findByRole("link", { name: "BTCUSDT" });

    await userEvent.selectOptions(screen.getByLabelText("Decision"), "VETOED");

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/signals?kind=VETOED"));
    await waitFor(() => expect(requested).toContain("/api/signals?kind=VETOED&limit=50"));
    expect(requested.some((u) => u.includes("cursor"))).toBe(false);
  });

  it("loads more rows by cursor and hides the button on the last page", async () => {
    const requested = stubApi({
      "/signals": (url: string) =>
        jsonResponse(
          url.includes("cursor=c1")
            ? page([makeRow({ id: ID_B, symbol: "ETHUSDT" })])
            : page([makeRow({ id: ID_A, symbol: "BTCUSDT" })], "c1"),
        ),
    });
    renderPage();
    await screen.findByRole("link", { name: "BTCUSDT" });

    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    expect(await screen.findByRole("link", { name: "ETHUSDT" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "BTCUSDT" })).toBeInTheDocument();
    expect(requested.at(-1)).toBe("/api/signals?limit=50&cursor=c1");
    expect(screen.queryByRole("button", { name: /load more/i })).not.toBeInTheDocument();
  });

  it("drops the extra pages when a filter changes after loading more", async () => {
    stubApi({
      "/signals": (url: string) => {
        if (url.includes("kind=VETOED")) return jsonResponse(page([makeRow({ id: ID_B, symbol: "SOLUSDT" })]));
        return jsonResponse(
          url.includes("cursor=c1")
            ? page([makeRow({ id: ID_B, symbol: "ETHUSDT" })])
            : page([makeRow({ id: ID_A, symbol: "BTCUSDT" })], "c1"),
        );
      },
    });
    renderPage();
    await screen.findByRole("link", { name: "BTCUSDT" });
    await userEvent.click(screen.getByRole("button", { name: /load more/i }));
    await screen.findByRole("link", { name: "ETHUSDT" });

    await userEvent.selectOptions(screen.getByLabelText("Decision"), "VETOED");

    expect(await screen.findByRole("link", { name: "SOLUSDT" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "BTCUSDT" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "ETHUSDT" })).not.toBeInTheDocument();
  });

  it("renders missing values as a dash, never null / NaN / undefined", async () => {
    stubApi({
      "/signals": page([
        makeRow({
          direction: null,
          outcome: null,
          realized_r: null,
          judge_ruling: null,
          session: null,
          violated_rule: null,
          entry_price: null,
        }),
      ]),
    });
    renderPage();
    const row = (await screen.findByRole("link", { name: "BTCUSDT" })).closest("tr") as HTMLElement;

    expect(within(row).getAllByText("—").length).toBeGreaterThanOrEqual(4);
    expect(row.textContent).not.toMatch(/null|NaN|undefined/);
  });

  it("shows an empty state when nothing matches", async () => {
    stubApi({ "/signals": page([]) });
    renderPage("/signals?symbol=XRPUSDT");
    expect(await screen.findByText("No signals match these filters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /export csv/i })).toBeDisabled();
  });

  it("keeps page one and shows an inline error when the next page fails", async () => {
    stubApi({
      "/signals": (url: string) =>
        url.includes("cursor=")
          ? jsonResponse({ reason: "bad_cursor" }, 400)
          : jsonResponse(page([makeRow({ id: ID_A, symbol: "BTCUSDT" })], "c1")),
    });
    renderPage();
    await screen.findByRole("link", { name: "BTCUSDT" });

    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    expect(await screen.findByText(/could not load more signals/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "BTCUSDT" })).toBeInTheDocument();
  });

  it("exports the loaded rows as CSV through a Blob download", async () => {
    const rows = [makeRow({ id: ID_A }), makeRow({ id: ID_B, symbol: "ETHUSDT" })];
    stubApi({ "/signals": page(rows) });
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((b: Blob) => {
      blobs.push(b);
      return "blob:csv";
    });
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    renderPage();
    await screen.findByRole("link", { name: "ETHUSDT" });

    await userEvent.click(screen.getByRole("button", { name: "Export CSV (2 rows)" }));

    expect(click).toHaveBeenCalledTimes(1);
    expect(blobs).toHaveLength(1);
    expect(await blobs[0]?.text()).toBe(rowsToCsv(rows));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:csv");
  });

  it("shows the waking state while Aurora resumes, then the rows", async () => {
    let calls = 0;
    stubApi({
      "/signals": () =>
        calls++ === 0 ? jsonResponse({ reason: "db_resuming" }, 503) : jsonResponse(page([makeRow()])),
    });
    renderPage("/signals", { client: createTestClient({ retry: 2, retryDelayMs: 300 }) });

    expect(await screen.findByText(/waking the database/i)).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "BTCUSDT" })).toBeInTheDocument();
  });
});
