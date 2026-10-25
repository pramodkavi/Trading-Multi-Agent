import { useDbProbe } from "../../api/queries";
import { Badge } from "../Badge";
import { QueryState } from "./QueryState";

/** The database probe: ok (with latency), waking, or error. */
export function DbPanel() {
  const query = useDbProbe();
  return (
    <QueryState query={query} label="database status">
      {(probe) => {
        if (probe.db === "ok") return <Badge tone="ok">{`Database ok · ${probe.latency_ms} ms`}</Badge>;
        if (probe.db === "resuming") return <Badge tone="warn">Database waking up</Badge>;
        return <Badge tone="bad">Database error</Badge>;
      }}
    </QueryState>
  );
}
