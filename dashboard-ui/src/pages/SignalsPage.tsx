import { useSearchParams } from "react-router-dom";
import { isDbResuming } from "../api/client";
import { useSignalsJournal } from "../api/queries";
import { buttonClass } from "../components/ErrorPage";
import { FilterBar } from "../components/journal/FilterBar";
import { JournalTable } from "../components/journal/JournalTable";
import { mutedClass } from "../components/styles";
import { rowsToCsv } from "../lib/csv";
import { filtersToParams, parseFilters } from "../lib/journalFilters";

function downloadCsv(csv: string): void {
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `signals-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function SignalsPage() {
  const [params, setParams] = useSearchParams();
  const filters = parseFilters(params);
  const query = useSignalsJournal(filters);
  const rows = query.data?.pages.flatMap((p) => p.items) ?? [];

  let body;
  if (query.data !== undefined) {
    body = (
      <>
        {query.isError ? (
          <p role="status" className="text-sm text-amber-900 dark:text-amber-200">
            {query.isFetchNextPageError
              ? "Could not load more signals — showing the rows loaded so far."
              : "Could not refresh signals — showing the last data received."}
          </p>
        ) : null}
        {rows.length === 0 ? (
          <p className={mutedClass}>No signals match these filters.</p>
        ) : (
          <JournalTable rows={rows} />
        )}
        <div className="flex flex-wrap items-center gap-3">
          {query.hasNextPage ? (
            <button
              type="button"
              className={buttonClass}
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              Load more
            </button>
          ) : null}
          <button
            type="button"
            className={buttonClass}
            disabled={rows.length === 0}
            onClick={() => downloadCsv(rowsToCsv(rows))}
          >
            {`Export CSV (${rows.length} ${rows.length === 1 ? "row" : "rows"})`}
          </button>
          {query.hasNextPage ? <span className={mutedClass}>Load more to include older rows.</span> : null}
        </div>
      </>
    );
  } else if (query.isPending) {
    body = (
      <p className={mutedClass}>
        {isDbResuming(query.failureReason) ? "Waking the database…" : "Loading signals…"}
      </p>
    );
  } else {
    body = (
      <div
        role="alert"
        className="rounded-lg border-2 border-red-600/50 bg-red-50 p-4 text-red-900 dark:border-red-400/50 dark:bg-red-950 dark:text-red-100"
      >
        <p className="font-semibold">Could not load signals</p>
        <p className="break-words text-sm">{query.error?.message}</p>
        <button type="button" className={`${buttonClass} mt-3`} onClick={() => void query.refetch()}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <section>
      <h1 className="mb-4 text-2xl font-semibold text-slate-900 dark:text-slate-100">Signals</h1>
      <div className="grid min-w-0 gap-4">
        <FilterBar value={filters} onChange={(next) => setParams(filtersToParams(next), { replace: true })} />
        {body}
      </div>
    </section>
  );
}
