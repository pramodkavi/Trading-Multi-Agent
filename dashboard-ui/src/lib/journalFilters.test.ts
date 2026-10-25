import { toApiQuery } from "./journalFilters";

describe("toApiQuery", () => {
  it("orders keys fixed, ends with limit, and omits empty filters", () => {
    expect(toApiQuery({ kind: "VETOED", symbol: "BTCUSDT" }, 50)).toBe(
      "symbol=BTCUSDT&kind=VETOED&limit=50",
    );
  });

  it("turns date-only bounds into UTC instants with an inclusive end day", () => {
    expect(toApiQuery({ since: "2026-10-01", until: "2026-10-25" }, 50)).toBe(
      "since=2026-10-01T00%3A00%3A00Z&until=2026-10-26T00%3A00%3A00Z&limit=50",
    );
  });

  it("appends an encoded cursor last", () => {
    expect(toApiQuery({}, 50, "a b/+=")).toBe("limit=50&cursor=a+b%2F%2B%3D");
  });
});
