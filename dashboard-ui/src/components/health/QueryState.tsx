import type { UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { isDbResuming } from "../../api/client";
import { mutedClass } from "../styles";

/** One panel's data lifecycle: waking / loading / its own inline error, else the content. */
export function QueryState<T>({
  query,
  label,
  children,
}: {
  query: UseQueryResult<T, Error>;
  label: string;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) {
    return (
      <p className={mutedClass}>
        {isDbResuming(query.failureReason) ? "Waking the database…" : "Loading…"}
      </p>
    );
  }
  if (query.isError) {
    return (
      <p role="status" className="text-sm text-red-800 dark:text-red-200">
        {`Could not load ${label}.`}
      </p>
    );
  }
  return <>{children(query.data)}</>;
}
