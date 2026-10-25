import { useState } from "react";
import { useAgentRun } from "../../api/queries";
import { buttonClass } from "../ErrorPage";
import { mutedClass } from "../styles";
import { JsonPanel } from "./JsonPanel";

/**
 * A step's full agent output, fetched only after the first time it is opened.
 * Closing keeps the data (the query stays enabled), so reopening never refetches.
 */
export function RawOutput({ signalId, runId }: { signalId: string; runId: string }) {
  const [open, setOpen] = useState(false);
  const [everOpened, setEverOpened] = useState(false);
  const run = useAgentRun(signalId, runId, everOpened);

  return (
    <div className="mt-2">
      <button
        type="button"
        className={buttonClass}
        aria-expanded={open}
        onClick={() => {
          setOpen((o) => !o);
          setEverOpened(true);
        }}
      >
        Raw output
      </button>
      {open ? (
        run.isPending ? (
          <p className={`${mutedClass} mt-2`}>Loading…</p>
        ) : run.isError ? (
          <p role="status" className="mt-2 text-sm text-red-800 dark:text-red-200">
            Could not load the raw output.
          </p>
        ) : (
          <JsonPanel value={run.data.output} />
        )
      ) : null}
    </div>
  );
}
