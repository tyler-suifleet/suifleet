import { bcs } from '@mysten/sui/bcs';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { normalizeStructTag, normalizeSuiAddress as id } from '@mysten/sui/utils';
import { FleetStorageBcs } from './fleet-budget';
import { RecordBcs } from './retention';

const ZERO = BigInt(0);
const Quota = bcs.struct('Quota', { period_ms: bcs.u64(), encoded_bytes_per_period: bcs.u64(), uploads_per_period: bcs.u64(), max_blob_size: bcs.u64(), write_fees_per_period: bcs.u64() });
const Usage = bcs.struct('Usage', { quota: Quota, period_start_ms: bcs.u64(), encoded_bytes_in_period: bcs.u64(), uploads_in_period: bcs.u64(), write_fees_in_period: bcs.u64(), total_encoded_bytes: bcs.u64(), total_uploads: bcs.u64() });
export interface DataStream {
  device: string | null;
  writtenEncodedBytes: bigint;
  retainedBytes: bigint;
  storedBytes: bigint;
  /** Byte milliseconds for currently fleet-held records within the requested window. */
  storedByteMs: bigint;
}
export interface MeterWindow { fromMs: number; toMs: number; epoch: number; firstEpochStartMs: number; epochDurationMs: number }

/** A fresh object/dynamic-field scan, never event discovery or a billing cache.
 * Device streams include all data kinds from that uploader; owner uploads are separate.
 * Releasing a blob removes its record but never its lifetime write counter.
 */
export async function readDataMeter(client: Pick<SuiGrpcClient, 'getObject' | 'listDynamicFields'>,
  storage: string, budget: string, origin: string, window: MeterWindow) {
  if (![window.fromMs, window.toMs, window.epoch, window.firstEpochStartMs, window.epochDurationMs].every(Number.isSafeInteger)
    || window.fromMs < 0 || window.toMs < window.fromMs || window.epoch < 0 || window.firstEpochStartMs < 0 || window.epochDurationMs <= 0) throw Error('Invalid metering window');
  const type = (name: string) => normalizeStructTag(`${origin}::fleet_storage::${name}`);
  const before = (await client.getObject({ objectId: storage, include: { content: true } })).object;
  if (before.owner.$kind !== 'Shared' || normalizeStructTag(before.type) !== type('FleetStorage')) throw Error('Unexpected fleet storage type/owner');
  const fleet = FleetStorageBcs.parse(before.content);
  if (id(fleet.id) !== id(storage) || id(fleet.budget_id) !== id(budget) || !['1', '2', '3'].includes(fleet.version)) throw Error('Fleet storage binding/version mismatch');
  const streams = new Map<string, DataStream>();
  const stream = (device: string | null) => {
    const key = device ? id(device) : 'owner';
    if (!streams.has(key)) streams.set(key, { device: device ? key : null, writtenEncodedBytes: ZERO, retainedBytes: ZERO, storedBytes: ZERO, storedByteMs: ZERO });
    return streams.get(key)!;
  };
  let cursor: string | null | undefined, records = ZERO, deviceWrites = ZERO;
  const cursors = new Set<string>(), keys = new Set<string>();
  do {
    const page = await client.listDynamicFields({ parentId: storage, cursor, limit: 100, include: { value: true } });
    if (cursors.size >= 3000 || page.dynamicFields.length > 100 || (page.hasNextPage && (!page.cursor || cursors.has(page.cursor)))) throw Error('Incomplete metering page');
    for (const field of page.dynamicFields) {
      const name = normalizeStructTag(field.name.type);
      if (name !== type('RecordKey') && name !== type('UsageKey')) continue;
      if (field.name.bcs.length !== 32) throw Error("Invalid metering key");
      const key = `${name}:${bcs.Address.parse(field.name.bcs)}`;
      if (keys.has(key) || keys.size >= 100000) throw Error('Duplicate or oversized metering scan');
      keys.add(key);
      if (field.$kind !== 'DynamicField' || normalizeStructTag(field.valueType) !== type(name === type('RecordKey') ? 'BlobRecord' : 'Usage')) throw Error('Unexpected metering field type');
      if (name === type('UsageKey')) {
        const usage = Usage.parse(field.value.bcs), row = stream(bcs.Address.parse(field.name.bcs));
        row.writtenEncodedBytes = BigInt(usage.total_encoded_bytes);
        deviceWrites += row.writtenEncodedBytes;
      } else {
        const record = RecordBcs.parse(field.value.bcs), row = stream(record.device_id);
        const bytes = BigInt(record.size), uploaded = Number(record.uploaded_at_ms);
        const end = window.firstEpochStartMs + record.end_epoch * window.epochDurationMs;
        if (!Number.isSafeInteger(uploaded) || !Number.isSafeInteger(end)) throw Error('Unsafe record time');
        row.retainedBytes += bytes;
        if (record.end_epoch > window.epoch) row.storedBytes += bytes;
        row.storedByteMs += bytes * BigInt(Math.max(0, Math.min(window.toMs, end) - Math.max(window.fromMs, uploaded)));
        records++;
      }
    }
    cursor = page.hasNextPage ? page.cursor : undefined;
    if (cursor) cursors.add(cursor);
  } while (cursor);
  // Detect any intervening upload/release/renewal; never publish a mixed snapshot.
  const after = (await client.getObject({ objectId: storage })).object;
  if (before.version !== after.version || before.digest !== after.digest) throw Error('Storage changed during scan; refresh usage');
  if (records !== BigInt(fleet.blob_count) || deviceWrites > BigInt(fleet.total_encoded_bytes)) throw Error('Storage counters do not reconcile');
  stream(null).writtenEncodedBytes = BigInt(fleet.total_encoded_bytes) - deviceWrites;
  const rows = [...streams.values()];
  const sum = (key: 'writtenEncodedBytes' | 'retainedBytes' | 'storedBytes' | 'storedByteMs') => rows.reduce((total, row) => total + row[key], ZERO);
  return { streams: rows, writtenEncodedBytes: sum('writtenEncodedBytes'), retainedBytes: sum('retainedBytes'), storedBytes: sum('storedBytes'), storedByteMs: sum('storedByteMs'), records };
}

