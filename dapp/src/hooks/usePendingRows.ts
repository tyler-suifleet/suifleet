"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { outstanding, parseSavedRows, type PendingRow } from "@/lib/pending-rows";
import { useFleetSelection } from "@/hooks/useFleetSelection";

const POLL_MS = 2_000;
const NONE: PendingRow[] = [];
/** Browser storage for unconfirmed rows; the key carries chain, registry, network, account and fleet. */
const SAVED = "suifleet:unconfirmed:v1:";

// Pending rows outlive the component that added them: switching tabs or
// screens must not re-enable an action whose change is still unlisted.
// Keyed by chain, account and fleet so another identity never sees them.
// Unconfirmed rows (SF-277) are also saved in browser storage so a reload or
// restart keeps the duplicate guard until the change is listed or the
// customer explicitly confirms they checked.
const rows = new Map<string, PendingRow[]>();
const hydrated = new Set<string>();
const listeners = new Set<() => void>();
function storage() { try { return typeof window === "undefined" ? undefined : window.localStorage; } catch { return undefined; } }
function hydrate(key: string) {
  if (hydrated.has(key)) return;
  const store = storage();
  if (!store) return;
  hydrated.add(key);
  let saved: PendingRow[] = [];
  try { saved = parseSavedRows(store.getItem(SAVED + key)); } catch { saved = []; }
  if (saved.length) rows.set(key, [...(rows.get(key) ?? NONE).filter((row) => !saved.some((s) => s.id === row.id)), ...saved]);
}
function persist(key: string, list: PendingRow[]) {
  const store = storage();
  if (!store) return;
  const keep = list.filter((row) => row.unconfirmed);
  try { if (keep.length) store.setItem(SAVED + key, JSON.stringify(keep)); else store.removeItem(SAVED + key); } catch { /* storage full or blocked: the in-memory guard remains */ }
}
function update(key: string, change: (current: PendingRow[]) => PendingRow[]) {
  hydrate(key);
  const next = change(rows.get(key) ?? NONE);
  if (next.length) rows.set(key, next); else rows.delete(key);
  persist(key, next);
  for (const listener of listeners) listener();
}
function subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; }
/** The selection identity without the role, so a role refresh never hides a saved guard. */
function scope(identity: string) { return identity.replace(/:(admin|support|read)$/, ""); }
function useRows(key: string) {
  hydrate(key);
  return useSyncExternalStore(subscribe, () => rows.get(key) ?? NONE, () => NONE);
}

/**
 * Pending rows for a list query: refetches every two seconds while a
 * confirmed change is not reflected yet, and drops rows once it is. Hooks
 * with the same `list` in the same fleet selection share their rows.
 */
export function usePendingRows(list: string, listed: string[] | undefined, refetch: () => unknown) {
  const key = `${scope(useFleetSelection().identity)}|${list}`;
  const pending = useRows(key);
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
  // Unconfirmed rows keep blocking a repeat after the polling window ends;
  // only a listed result or an explicit dismissal releases them.
  const blocking = [...waiting, ...stale.filter((row) => row.unconfirmed)];
  return { waiting, stale, blocking, add, dismiss };
}

/**
 * A saved guard for an action whose result has no ID to watch (for example a
 * new group or deployment). It is set before the wallet can submit and stays
 * until the action is confirmed, definitely failed, or the customer confirms
 * they checked; reloads and remounts keep it.
 */
export function useUnconfirmedAction(name: string) {
  const key = `${scope(useFleetSelection().identity)}|action:${name}`;
  const row = useRows(key)[0] as PendingRow | undefined;
  const mark = useCallback((label: string) => update(key, () => [{ id: name, label, kind: "create", since: Date.now(), unconfirmed: true }]), [key, name]);
  const clear = useCallback(() => update(key, () => NONE), [key]);
  return { row, mark, clear };
}
