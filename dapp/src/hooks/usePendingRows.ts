"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { outstanding, type PendingRow } from "@/lib/pending-rows";
import { useFleetSelection } from "@/hooks/useFleetSelection";

const POLL_MS = 2_000;
const NONE: PendingRow[] = [];

// Pending rows outlive the component that added them: switching tabs or
// screens must not re-enable an action whose change is still unlisted.
// Keyed by chain, account and fleet so another identity never sees them.
const rows = new Map<string, PendingRow[]>();
const listeners = new Set<() => void>();
function update(key: string, change: (current: PendingRow[]) => PendingRow[]) {
  const next = change(rows.get(key) ?? NONE);
  if (next.length) rows.set(key, next); else rows.delete(key);
  for (const listener of listeners) listener();
}
function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }

/**
 * Pending rows for a list query: refetches every two seconds while a
 * confirmed change is not reflected yet, and drops rows once it is. Hooks
 * with the same `list` in the same fleet selection share their rows.
 */
export function usePendingRows(list: string, listed: string[] | undefined, refetch: () => unknown) {
  const key = `${useFleetSelection().identity}|${list}`;
  const pending = useSyncExternalStore(subscribe, () => rows.get(key) ?? NONE, () => NONE);
  const [now, setNow] = useState(() => Date.now());
  const { open, waiting, stale } = outstanding(pending, listed, now);
  const refresh = useRef(refetch); refresh.current = refetch;
  useEffect(() => { if (open.length !== pending.length) update(key, (current) => current.filter((row) => open.includes(row))); }, [key, open, pending.length]);
  const polling = waiting.length > 0;
  useEffect(() => {
    if (!polling) return;
    const timer = setInterval(() => { setNow(Date.now()); void refresh.current(); }, POLL_MS);
    return () => clearInterval(timer);
  }, [polling]);
  const add = useCallback((added: Omit<PendingRow, "since">[]) => {
    const since = Date.now();
    setNow(since);
    update(key, (current) => [...current.filter((row) => !added.some((r) => r.id === row.id)), ...added.map((row) => ({ ...row, since }))]);
    void refresh.current();
  }, [key]);
  const dismiss = useCallback((id: string) => update(key, (current) => current.filter((row) => row.id !== id)), [key]);
  return { waiting, stale, add, dismiss };
}
