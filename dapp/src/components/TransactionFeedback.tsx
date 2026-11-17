"use client";

import { useCallback, useState } from "react";
import { describeTransactionFailure, type TransactionFeedback as Feedback } from "@/lib/tx-feedback";

/** Inline guidance for a failed fleet transaction, with optional technical details. */
export function TransactionFeedback({ feedback, onDismiss }: { feedback: Feedback | null; onDismiss?: () => void }) {
  if (!feedback) return null;
  const tone = feedback.stage === "uncertain" ? "border-amber-300 bg-amber-50 text-amber-900" : "border-red-200 bg-red-50 text-red-900";
  return <div role="alert" data-stage={feedback.stage} data-kind={feedback.kind} className={`rounded border px-3 py-2 text-sm ${tone}`}>
    <p className="font-medium">{feedback.title}</p>
    <p className="mt-1">{feedback.next}</p>
    {feedback.digest && <p className="mt-1 break-all text-xs">Transaction: <span className="font-mono">{feedback.digest}</span></p>}
    <div className="mt-1 flex flex-wrap items-center gap-3">
      <details className="text-xs"><summary className="cursor-pointer">Technical details</summary><pre className="mt-1 whitespace-pre-wrap break-all">{feedback.details}</pre></details>
      {onDismiss && <button type="button" className="text-xs underline" onClick={onDismiss}>Dismiss</button>}
    </div>
  </div>;
}

/** Per-screen state for the latest transaction failure. */
export function useTransactionFeedback() {
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const report = useCallback((error: unknown, action?: string) => {
    const described = describeTransactionFailure(error, action);
    setFeedback(described);
    return described;
  }, []);
  const clear = useCallback(() => setFeedback(null), []);
  return { feedback, report, clear };
}
