// Log search over verified, decrypted fleet archives (SF-084) and the
// session's record of archive access. Plaintext exists only in page memory:
// nothing here persists, caches or transmits records.
import type { Archive, ArchiveRecord } from "./fleet-bulk";

export const MAX_QUERY_BYTES = 256;
export const MAX_MATCHES = 1000;

export interface LogQuery {
  /** Plain substring; never compiled as a regular expression. */
  text: string;
  /** `daemon`, `component:NAME`, or empty for every source. */
  source: string;
  caseSensitive: boolean;
}

export interface LogMatch extends ArchiveRecord { index: number }

export function validateQuery(query: LogQuery): void {
  if (new TextEncoder().encode(query.text).length > MAX_QUERY_BYTES) throw new Error(`Search text is limited to ${MAX_QUERY_BYTES} bytes`);
  if (query.source && query.source !== "daemon" && !(query.source.startsWith("component:") && query.source.length > 10)) {
    throw new Error("Unknown log source");
  }
}

/** Matching records of one verified archive, in order, at most MAX_MATCHES. */
export function searchArchive(archive: Archive, query: LogQuery): { matches: LogMatch[]; truncated: boolean } {
  validateQuery(query);
  const needle = query.caseSensitive ? query.text : query.text.toLowerCase();
  const matches: LogMatch[] = [];
  for (let index = 0; index < archive.records.length; index++) {
    const record = archive.records[index];
    if (query.source && record.source !== query.source) continue;
    const line = query.caseSensitive ? record.line : record.line.toLowerCase();
    if (needle && !line.includes(needle)) continue;
    if (matches.length === MAX_MATCHES) return { matches, truncated: true };
    matches.push({ ...record, index });
  }
  return { matches, truncated: false };
}

/** Distinct sources in an archive, for the source filter. */
export function archiveSources(archive: Archive): string[] {
  return [...new Set(archive.records.map((r) => r.source))].sort();
}

export interface AccessEntry {
  atMs: number;
  account: string;
  authority: "owner" | "support";
  device: string;
  index: number;
  kind: "logs" | "diagnostics" | "crash";
  outcome: "opened" | "refused";
}

export const MAX_ACCESS_ENTRIES = 200;

/** Append to the in-memory session record, keeping the newest entries. */
export function recordAccess(log: readonly AccessEntry[], entry: AccessEntry): AccessEntry[] {
  return [entry, ...log].slice(0, MAX_ACCESS_ENTRIES);
}
