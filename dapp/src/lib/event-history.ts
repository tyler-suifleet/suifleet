// Durable event discovery. Objects remain authoritative; this cache is only IDs
// and historical events. Never turn a pruned interval into an empty success.
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import type { SuiClientTypes } from '@mysten/sui/client';
import { fromBase64, toBase64, normalizeSuiAddress } from '@mysten/sui/utils';
import { requireManifest } from './deployment-manifest';

type Event = SuiClientTypes.EventEntry;
type Scan = { version: 1; cursor?: string; checkpoint: string; partial: boolean; events: Event[] };
const pending = new WeakMap<SuiGrpcClient, Map<string, Promise<Event[]>>>();
const memory = new WeakMap<SuiGrpcClient, Map<string, Scan>>();
const warnings = new Map<string, string>();
const listeners = new Set<() => void>();
let snapshot = '';
export const historySnapshot = () => snapshot;
export const subscribeHistory = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
function warn(key: string, message: string) {
  warnings.set(key, message);
  snapshot = [...new Set(warnings.values())].join(' ');
  listeners.forEach(listener => listener());
}
function storage() { try { return typeof window === 'undefined' ? undefined : window.localStorage; } catch { return undefined; } }
const PREFIX = 'suifleet-history-v1:';
const STREAM_LIMIT = 256 * 1024;
const ORIGIN_LIMIT = 2 * 1024 * 1024;
const encode = (scan: Scan) => JSON.stringify(scan, (name, value) => name === 'bcs' ? toBase64(value) : value);
function restore(key: string): Scan | undefined {
  const value = storage()?.getItem(key);
  if (!value) return;
  if (value.length * 2 > STREAM_LIMIT) throw Error('Oversized saved history');
  const parsed = JSON.parse(value, (name, value) => name === 'bcs' ? fromBase64(value) : value) as Scan;
  if (parsed.version !== 1 || !/^\d+$/.test(parsed.checkpoint) || !Array.isArray(parsed.events)
    || parsed.events.some(event => !(event.bcs instanceof Uint8Array) || typeof event.transactionDigest !== 'string' || typeof event.eventIndex !== 'number' || typeof event.checkpoint !== 'string')
    || typeof parsed.partial !== 'boolean' || (parsed.cursor !== undefined && typeof parsed.cursor !== 'string')) throw Error('Invalid saved history');
  return parsed;
}
function persist(key: string, scan: Scan) {
  // Bound memory as well as storage. Evicted discovery is always explicitly partial.
  while (encode(scan).length * 2 > STREAM_LIMIT && scan.events.length) {
    scan.events.shift();
    scan.partial = true;
    warn(key, 'History unavailable: discovery cache capacity reached; older objects may be omitted. Use an archival indexer.');
  }
  try {
    const local = storage();
    if (!local) throw Error('storage unavailable');
    const value = encode(scan);
    const keys = Array.from({ length: local.length }, (_, i) => local.key(i)).filter((k): k is string => !!k && k.startsWith(PREFIX) && k !== key);
    let used = keys.reduce((n, k) => n + (k.length + (local.getItem(k)?.length ?? 0)) * 2, (key.length + value.length) * 2);
    for (const old of keys) {
      if (used <= ORIGIN_LIMIT) break;
      used -= (old.length + (local.getItem(old)?.length ?? 0)) * 2;
      local.removeItem(old);
      warn(old, 'History unavailable: cached discovery evicted; this stream will rescan the retained range.');
    }
    local.setItem(key, value);
  } catch {
    warn(key, 'History unavailable on reload: browser storage is full or inaccessible. Discovery continues in memory; free storage to retain progress.');
  }
}

export async function incrementalEvents(client: SuiGrpcClient, type: string, sender?: string): Promise<Event[]> {
  const manifest = requireManifest();
  sender = sender ? normalizeSuiAddress(sender) : undefined;
  const key = `suifleet-history-v1:${manifest.chain_identifier}:${manifest.rpc_url}:${manifest.call_package_id}:${type}:${sender ?? '*'}`;
  let tasks = pending.get(client);
  if (!tasks) pending.set(client, tasks = new Map());
  const existing = tasks.get(key);
  if (existing) return existing;
  const task = scan(client, key, type, sender).catch(error => {
    warn(key, error instanceof Error ? error.message : 'History unavailable');
    throw error;
  }).finally(() => tasks!.delete(key));
  tasks.set(key, task);
  return task;
}

async function scan(client: SuiGrpcClient, key: string, type: string, sender?: string): Promise<Event[]> {
  const { response: info } = await client.ledgerService.getServiceInfo({});
  if (info.lowestAvailableCheckpoint === undefined) throw Error('History unavailable: endpoint does not report its retained checkpoint range');
  const lowest = info.lowestAvailableCheckpoint;
  let cache = memory.get(client);
  if (!cache) memory.set(client, cache = new Map());
  let invalid = false;
  let saved = cache.get(key);
  if (!saved) {
    try { saved = restore(key); }
    catch { invalid = true; warn(key, 'History unavailable: invalid saved discovery; rescanning the retained range.'); }
  }
  if (saved && BigInt(saved.checkpoint) + BigInt(1) < lowest) {
    saved = { ...saved, cursor: undefined, checkpoint: lowest.toString(), partial: true };
  }
  // Initial public-node discovery is explicitly partial. Do not label it complete.
  const next: Scan = saved ? { ...saved, events: [...saved.events] } : { version: 1, checkpoint: lowest.toString(), partial: invalid || lowest > BigInt(0), events: [] };
  if (next.partial) warn(key, `History unavailable before checkpoint ${lowest}; event-based discovery may omit older or evicted objects. Use an archival endpoint for complete history.`);
  const known = new Set(next.events.map(event => `${event.transactionDigest}:${event.eventIndex}`));
  for (let pages = 0; ; pages++) {
    if (pages >= 200) {
      warn(key, 'History still loading: partial discovery saved; the next refresh resumes from this checkpoint.');
      persist(key, next); cache.set(key, next);
      return [...next.events].reverse();
    }
    const before = next.cursor;
    const call = client.ledgerService.listEvents({
      startCheckpoint: lowest,
      readMask: { paths: ['package_id', 'module', 'sender', 'event_type', 'contents', 'checkpoint', 'transaction_digest', 'event_index'] },
      filter: { terms: [{ literals: [
        { negated: false, predicate: { oneofKind: 'eventType', eventType: { eventType: type } } },
        ...(sender ? [{ negated: false, predicate: { oneofKind: 'sender' as const, sender: { address: sender } } }] : []),
      ] }] },
      options: { ordering: 0, limit: 50, after: before ? fromBase64(before) : undefined },
    });
    let reason: number | undefined;
    for await (const frame of call.responses) {
      if (frame.watermark?.cursor) next.cursor = toBase64(frame.watermark.cursor);
      if (frame.watermark?.checkpoint !== undefined) next.checkpoint = frame.watermark.checkpoint.toString();
      const event = frame.event;
      if (event) {
        if (!event.transactionDigest || event.eventIndex === undefined || !event.contents?.value || event.checkpoint === undefined) throw Error('Incomplete history event');
        const id = `${event.transactionDigest}:${event.eventIndex}`;
        if (!known.has(id)) {
          known.add(id);
          next.events.push({ packageId: event.packageId!, module: event.module!, sender: event.sender!, eventType: event.eventType!,
            bcs: event.contents.value, json: null, checkpoint: event.checkpoint.toString(), transactionDigest: event.transactionDigest, eventIndex: event.eventIndex });
        }
      }
      if (frame.end) reason = frame.end.reason;
    }
    if (reason === undefined || reason === 0) throw Error('History stream ended without a successful terminal frame');
    if (reason === 1 || reason === 2) {
      if (!next.cursor || next.cursor === before) throw Error('Event pagination made no progress or returned no cursor');
      persist(key, next); cache.set(key, { ...next, events: [...next.events] });
      continue;
    }
    if (reason !== 3 && reason !== 5) throw Error('Unexpected history scan boundary');
    // Watermarks include safe progress even when no matching event was emitted.
    if (!next.cursor) throw Error('History unavailable: endpoint returned no scan watermark');
    if (!next.partial) { warnings.delete(key); snapshot = [...new Set(warnings.values())].join(' '); listeners.forEach(listener => listener()); }
    persist(key, next);
    cache.set(key, next);
    return [...next.events].reverse();
  }
}
