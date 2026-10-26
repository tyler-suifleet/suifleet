"use client";

import { useSyncExternalStore } from "react";
import { historySnapshot, subscribeHistory } from "@/lib/event-history";

/** Secondary disclosure: preserve incomplete-history evidence without a global alert banner. */
export function HistoryStatus() {
  const message = useSyncExternalStore(subscribeHistory, historySnapshot, () => "");
  return <HistoryStatusView message={message} />;
}

/** Presentation is separate so disclosure behavior can be checked without network or wallet fixtures. */
export function HistoryStatusView({ message }: { message: string }) {
  if (!message) return null;
  return <footer className="mt-10 border-t border-slate-200 pt-4 text-xs text-slate-600">
    <details className="history-status max-w-3xl">
      <summary className="inline-flex min-h-9 cursor-pointer items-center gap-2 rounded-md px-2 font-medium hover:bg-slate-100">
        <span aria-hidden="true" className="h-2 w-2 rounded-full bg-amber-500" />
        Data status · some history may be missing
      </summary>
      <div className="mt-2 space-y-3 rounded-lg border border-slate-200 bg-white p-4 text-sm leading-relaxed">
        <p>Your connection or browser storage limits the history available here. Older devices, groups or updates may be missing from lists and totals. An empty list does not confirm that no records exist.</p>
        <p>If you expect older records, ask your fleet administrator to check the connection’s history coverage and browser storage. Complete network history requires a connection that retains older records.</p>
        <details><summary className="cursor-pointer font-medium text-slate-700">Technical details for your administrator</summary>
          <p className="mt-2 whitespace-pre-wrap break-words text-xs text-slate-500">{message}</p>
        </details>
      </div>
    </details>
  </footer>;
}
