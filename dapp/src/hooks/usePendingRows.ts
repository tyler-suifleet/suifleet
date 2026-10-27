"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { outstanding, type PendingRow } from "@/lib/pending-rows";

const POLL_MS = 2_000;

/**
 * Pending rows for a list query: refetches every two seconds while a
 * confirmed change is not reflected yet, and drops rows once it is.
 */
export function usePendingRows(listed: string[] | undefined, refetch: () => unknown) {
  const [pending, setPending] = useState<PendingRow[]>([]);
  const [now, setNow] = useState(() => Date.now());
  const { open, waiting, stale } = outstanding(pending, listed, now);
  const refresh = useRef(refetch); refresh.current = refetch;
  useEffect(() => { if (open.length !== pending.length) setPending(open); }, [open, pending.length]);
  const polling = waiting.length > 0;
  useEffect(() => {
    if (!polling) return;
    const timer = setInterval(() => { setNow(Date.now()); void refresh.current(); }, POLL_MS);
    return () => clearInterval(timer);
  }, [polling]);
  const add = useCallback((rows: Omit<PendingRow, "since">[]) => {
    const since = Date.now();
    setNow(since);
    setPending((current) => [...current.filter((row) => !rows.some((r) => r.id === row.id)), ...rows.map((row) => ({ ...row, since }))]);
    void refresh.current();
  }, []);
  const dismiss = useCallback((id: string) => setPending((current) => current.filter((row) => row.id !== id)), []);
  return { waiting, stale, add, dismiss };
}
