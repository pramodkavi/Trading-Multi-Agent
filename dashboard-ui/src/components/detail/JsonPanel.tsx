import { useState } from "react";
import { buttonClass } from "../ErrorPage";

/** Pretty-printed JSON, scrollable inside its box; rendered as text, never as HTML. */
export function JsonPanel({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2) ?? "null";
  const [copied, setCopied] = useState(false);
  const canCopy = typeof navigator !== "undefined" && typeof navigator.clipboard?.writeText === "function";
  return (
    <div className="mt-2">
      <pre className="max-h-96 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 text-xs text-slate-900 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100">
        <code>{text}</code>
      </pre>
      {canCopy ? (
        <button
          type="button"
          className={`${buttonClass} mt-2`}
          onClick={() => {
            void navigator.clipboard.writeText(text).then(() => setCopied(true));
          }}
        >
          {copied ? "Copied" : "Copy JSON"}
        </button>
      ) : null}
    </div>
  );
}
