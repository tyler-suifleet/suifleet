import { normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";

/**
 * A row a confirmed transaction created or deleted that the list query does
 * not reflect yet. Event indexing can lag execution by several seconds; the
 * row keeps the change visible, and the action that made it disabled, until
 * the list catches up.
 */
export interface PendingRow { id: string; label: string; kind: "create" | "delete"; since: number }

/** After this long a pending row stops polling and asks for a later refresh. */
export const PENDING_TIMEOUT_MS = 60_000;

/**
 * Split `pending` against the listed IDs: creations not yet listed and
 * deletions still listed are outstanding; the rest have settled. Until the
 * list has loaded (`listed` undefined) every row stays outstanding.
 */
export function outstanding(pending: PendingRow[], listed: string[] | undefined, now: number) {
  const ids = listed && new Set(listed.map((id) => normalizeSuiAddress(id)));
  const open = pending.filter((row) => !ids || ids.has(normalizeSuiAddress(row.id)) === (row.kind === "delete"));
  return {
    open,
    waiting: open.filter((row) => now - row.since < PENDING_TIMEOUT_MS),
    stale: open.filter((row) => now - row.since >= PENDING_TIMEOUT_MS),
  };
}

/** IDs read from confirmed transaction events of one Move type. */
export function eventIds(events: readonly { eventType: string; bcs: Uint8Array }[], type: string, read: (bcs: Uint8Array) => string): string[] {
  const wanted = normalizeStructTag(type);
  return events.filter((event) => normalizeStructTag(event.eventType) === wanted).map((event) => normalizeSuiAddress(read(event.bcs)));
}
