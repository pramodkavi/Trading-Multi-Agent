import type { DecisionKind, SignalOutcome } from "../api/types";
import type { Tone } from "./problems";

/** Badge tone per derived decision kind (shared by Overview, Journal and Detail). */
export const KIND_TONE: Record<DecisionKind, Tone> = {
  PUBLISHED: "ok",
  VETOED: "warn",
  GATE_REJECTED: "neutral",
  NO_SETUP: "neutral",
};

/** Badge tone per terminal signal outcome. */
export function outcomeTone(outcome: SignalOutcome | string | null): Tone {
  if (outcome === "WIN") return "ok";
  if (outcome === "LOSS") return "bad";
  return "neutral";
}
