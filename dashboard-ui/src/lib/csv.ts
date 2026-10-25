import type { SignalRowOut } from "../api/types";

const HEADER = [
  "created_at",
  "symbol",
  "direction",
  "decision_kind",
  "judge_ruling",
  "outcome",
  "session",
  "entry_price",
  "stop_loss",
  "take_profit_1",
  "risk_reward_ratio",
  "realized_r",
  "violated_rule",
  "tags",
  "id",
] as const;

/**
 * One CSV cell (RFC 4180). Text starting with `=`, `+`, `-`, `@`, tab or CR is prefixed
 * with `'` so a spreadsheet never evaluates it as a formula; numbers are written as-is.
 */
export function csvCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  let text = Array.isArray(value) ? value.map(String).join(";") : String(value);
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

/** The journal rows as CSV text: fixed header, CRLF line endings, never the skip details. */
export function rowsToCsv(rows: SignalRowOut[]): string {
  const lines = [HEADER.join(",")];
  for (const row of rows) {
    lines.push(HEADER.map((key) => csvCell(row[key])).join(","));
  }
  return `${lines.join("\r\n")}\r\n`;
}
