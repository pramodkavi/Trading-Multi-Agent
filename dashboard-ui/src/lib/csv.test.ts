import { makeRow } from "../test/fixtures";
import { csvCell, rowsToCsv } from "./csv";

describe("csvCell", () => {
  it("quotes cells with commas, quotes and newlines, doubling quotes", () => {
    expect(csvCell('He said "hi", ok\nbye')).toBe('"He said ""hi"", ok\nbye"');
  });

  it("neutralises spreadsheet formulas in text", () => {
    expect(csvCell("=SUM(A1)")).toBe("'=SUM(A1)");
    expect(csvCell("+1")).toBe("'+1");
    expect(csvCell("@cmd")).toBe("'@cmd");
    expect(csvCell("-x")).toBe("'-x");
    expect(csvCell("\tx")).toBe("'\tx");
  });

  it("leaves numbers (even negative), unicode and plain text alone, and null empty", () => {
    expect(csvCell(-0.5)).toBe("-0.5");
    expect(csvCell("é — ok")).toBe("é — ok");
    expect(csvCell(null)).toBe("");
    expect(csvCell(undefined)).toBe("");
  });

  it("joins arrays with semicolons", () => {
    expect(csvCell(["a", "b"])).toBe("a;b");
  });
});

describe("rowsToCsv", () => {
  it("writes the fixed header and CRLF line endings", () => {
    const csv = rowsToCsv([makeRow({ tags: ["a", "b"], realized_r: -1 })]);
    const lines = csv.split("\r\n");
    expect(lines[0]).toBe(
      "created_at,symbol,direction,decision_kind,judge_ruling,outcome,session,entry_price,stop_loss,take_profit_1,risk_reward_ratio,realized_r,violated_rule,tags,id",
    );
    expect(lines[1]).toBe(
      "2026-10-25T08:03:00Z,BTCUSDT,LONG,PUBLISHED,PUBLISH,,LONDON,100,95,115,3,-1,,a;b,11111111-1111-4111-8111-111111111111",
    );
    expect(csv.endsWith("\r\n")).toBe(true);
  });

  it("never exports skip details and guards a hostile rule name", () => {
    const csv = rowsToCsv([makeRow({ skip_details: "secret", violated_rule: "=cmd|calc" })]);
    expect(csv).not.toContain("secret");
    expect(csv).toContain("'=cmd|calc");
  });

  it("exports just the header for no rows", () => {
    expect(rowsToCsv([]).split("\r\n")).toHaveLength(2);
  });
});
