const pad2 = (n: number) => String(n).padStart(2, "0");

function utcParts(iso: string): { month: string; day: string; hh: string; mm: string } | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return {
    month: pad2(d.getUTCMonth() + 1),
    day: pad2(d.getUTCDate()),
    hh: pad2(d.getUTCHours()),
    mm: pad2(d.getUTCMinutes()),
  };
}

/** `08:03` — the UTC wall-clock time of an ISO instant; `—` when unparseable. */
export function formatUtcTime(iso: string): string {
  const p = utcParts(iso);
  return p ? `${p.hh}:${p.mm}` : "—";
}

/** `10-25 08:03` — UTC month-day and time; `—` when unparseable. */
export function formatUtcDateTime(iso: string): string {
  const p = utcParts(iso);
  return p ? `${p.month}-${p.day} ${p.hh}:${p.mm}` : "—";
}

/** `+1.25 R` / `−0.40 R` (true minus) / `—` when unknown. */
export function formatR(r: number | null): string {
  if (r === null || !Number.isFinite(r)) return "—";
  const rounded = Math.round(r * 100) / 100;
  if (rounded === 0) return "0.00 R";
  const body = Math.abs(rounded).toFixed(2);
  return rounded > 0 ? `+${body} R` : `−${body} R`;
}

/** Elapsed seconds as `42 s` or `3 m 05 s`; `—` when null. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} s`;
  return `${Math.floor(total / 60)} m ${pad2(total % 60)} s`;
}

/** Time until an event: `2 h 05 m`, `2 m 05 s`, `45 s`, or `now` once due. */
export function formatCountdown(seconds: number): string {
  const total = Math.floor(seconds);
  if (!Number.isFinite(total) || total <= 0) return "now";
  if (total >= 3600) return `${Math.floor(total / 3600)} h ${pad2(Math.floor((total % 3600) / 60))} m`;
  if (total >= 60) return `${Math.floor(total / 60)} m ${pad2(total % 60)} s`;
  return `${total} s`;
}

/** Shorten to at most `max` characters, ending in an ellipsis when cut. */
export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

/** How long ago something happened: `30 s ago`, `5 min ago`, `2 h ago`; `never` when null. */
export function formatAge(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "never";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} s ago`;
  if (total < 3600) return `${Math.round(total / 60)} min ago`;
  return `${Math.round(total / 3600)} h ago`;
}
