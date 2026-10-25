import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { HealthStrip as HealthStripData } from "../../api/types";
import type { Tone } from "../../lib/problems";
import { Badge } from "../Badge";

const SCAN_TONE = { SUCCESS: "ok", FAILED: "bad", RUNNING: "warn" } as const;

/** Non-ok badges link to the Health page (design §4.2 P1). */
function Linked({ tone, children }: { tone: Tone; children: ReactNode }) {
  return tone === "ok" ? <>{children}</> : <Link to="/health">{children}</Link>;
}

/** One badge per concern: last scan, Forecaster coverage, alarms, database. */
export function HealthStrip({ health }: { health: HealthStripData }) {
  const { last_scan: scan, forecaster, alarms } = health;

  const scanTone: Tone = scan ? SCAN_TONE[scan.status] : "neutral";
  const forecasterTone: Tone = forecaster.stale > 0 || forecaster.errors_last_window > 0 ? "warn" : "ok";

  let alarmTone: Tone;
  let alarmLabel: string;
  if (alarms === null) {
    alarmTone = "warn";
    alarmLabel = "Alarms unavailable";
  } else if (alarms.some((a) => a.state === "ALARM")) {
    alarmTone = "bad";
    alarmLabel = `Alarms: ${alarms.filter((a) => a.state === "ALARM").length} in ALARM`;
  } else if (alarms.some((a) => a.state === "INSUFFICIENT_DATA")) {
    alarmTone = "warn";
    alarmLabel = "Alarms: insufficient data";
  } else {
    alarmTone = "ok";
    alarmLabel = "Alarms ok";
  }

  return (
    <div data-testid="health-strip" className="flex flex-wrap gap-2">
      <Linked tone={scanTone}>
        <Badge tone={scanTone}>{scan ? `Last scan ${scan.label} · ${scan.status}` : "No scan yet"}</Badge>
      </Linked>
      <Linked tone={forecasterTone}>
        <Badge tone={forecasterTone}>
          {forecasterTone === "ok"
            ? "Forecaster ok"
            : `Forecaster: ${forecaster.stale} stale, ${forecaster.errors_last_window} errors`}
        </Badge>
      </Linked>
      <Linked tone={alarmTone}>
        <Badge tone={alarmTone} title={health.alarms_note ?? undefined}>
          {alarmLabel}
        </Badge>
      </Linked>
      <Badge tone="ok">DB ok</Badge>
    </div>
  );
}
