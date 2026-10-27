// SF-141 archive stream policy. Epochs are Walrus epochs, never Sui epochs.
import { bcs } from '@mysten/sui/bcs';
import { ObjectError } from '@mysten/sui/client';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { normalizeSuiAddress as id, normalizeStructTag } from '@mysten/sui/utils';
import { Transaction } from '@mysten/sui/transactions';
import { readFleetData, type FleetDataConfig } from './fleet-data';
import { FleetStorageBcs, type StorageConfig } from './fleet-budget';
import { RecordBcs } from './retention';
import type { Anchor } from './fleet-bulk';
import type { ArchivePlan } from './storage-reclaim';
export const ARCHIVE_STREAMS = ['Logs', 'Diagnostics', 'Crash'] as const;
export const StreamRetentionBcs = bcs.struct('StreamRetention', { epochs: bcs.u32() });
export function validateStreamRetention(kind: number, epochs: number) {
  if (!Number.isInteger(kind) || kind < 0 || kind > 2 || !Number.isInteger(epochs) || epochs < 1 || epochs > 53) throw Error('Choose an archive stream and 1–53 Walrus epochs');
}
export async function readStreamRetention(client: Pick<SuiGrpcClient, 'getDynamicField'>, config: FleetDataConfig, kind: number): Promise<number | undefined> {
  validateStreamRetention(kind, 1);
  if (!config.streamRetentionOrigin) throw Error('Stream retention upgrade is not configured');
  try {
    const { dynamicField: field } = await client.getDynamicField({ parentId: config.fleetId, name: {
      type: `${config.streamRetentionOrigin}::fleet_data::StreamRetentionKey`, bcs: bcs.u8().serialize(kind).toBytes() } });
    if (normalizeStructTag(field.value.type) !== normalizeStructTag(`${config.streamRetentionOrigin}::fleet_data::StreamRetention`)) throw Error('Unexpected retention type');
    const { epochs } = StreamRetentionBcs.parse(field.value.bcs); validateStreamRetention(kind, epochs); return epochs;
  } catch (error) {
    if (error instanceof ObjectError && error.reason === 'notFound') return undefined;
    throw error;
  }
}
export function setStreamRetentionTx(config: FleetDataConfig, cap: string, kind: number, epochs: number) {
  validateStreamRetention(kind, epochs);
  if (!config.streamRetentionOrigin) throw Error('Stream retention upgrade is not configured');
  const tx = new Transaction();
  tx.moveCall({ target: `${config.packageId}::fleet_data::set_stream_retention`, arguments: [tx.object(config.fleetId), tx.object(config.budgetId), tx.object(cap), tx.pure.u8(kind), tx.pure.u32(epochs)] });
  return tx;
}
/** Automatic selection never includes unanchored uploads: they may represent
 * an offline device's only ciphertext while its raw spool still needs custody.
 * Owner-reviewed loose-blob cleanup remains the explicit SF-129 operation. */
export function streamExpiryPlan(plan: ArchivePlan, kind: number): ArchivePlan {
  validateStreamRetention(kind, 1);
  return { ...plan, anchors: plan.anchors.filter(c => c.anchor.kind === kind), loose: [] };
}
/** Reads the chain binding before trusting optional introduction-package fields. */
export async function readStreamPolicies(client: Parameters<typeof readFleetData>[0], config: FleetDataConfig) {
  await readFleetData(client, config);
  return Promise.all(ARCHIVE_STREAMS.map((_, kind) => readStreamRetention(client, config, kind)));
}

/** Drop expired/released data from the live index without deleting anything.
 * Live storage metadata includes later owner extensions; the original anchor
 * expiry alone never authorizes removal. Corrupt/unreadable metadata fails closed.
 */
export async function liveArchiveEntries(client: Pick<SuiGrpcClient, 'getObject' | 'getDynamicField'>,
  config: FleetDataConfig, device: string, entries: {index: number; anchor: Anchor}[], epoch: number) {
  if (!Number.isSafeInteger(epoch) || epoch < 0) throw Error('Invalid Walrus epoch');
  const { object } = await client.getObject({ objectId: config.storageId, include: { content: true } });
  const [origin, module, name] = normalizeStructTag(object.type).split('::');
  if (module !== 'fleet_storage' || name !== 'FleetStorage' || object.owner.$kind !== 'Shared') throw Error('Invalid fleet storage');
  const storage = FleetStorageBcs.parse(object.content);
  if (id(storage.id) !== config.storageId || id(storage.budget_id) !== config.budgetId) throw Error('Foreign storage');
  const live = [];
  for (const entry of entries) {
    let field;
    try { field = (await client.getDynamicField({ parentId: config.storageId, name: { type: `${origin}::fleet_storage::RecordKey`, bcs: bcs.Address.serialize(entry.anchor.blob_object_id).toBytes() } })).dynamicField; }
    catch (error) { if (error instanceof ObjectError && error.reason === 'notFound') continue; throw error; }
    if (normalizeStructTag(field.value.type) !== normalizeStructTag(`${origin}::fleet_storage::BlobRecord`)) throw Error('Invalid blob record');
    const record = RecordBcs.parse(field.value.bcs);
    if (!record.device_id || id(record.device_id) !== id(device) || BigInt(record.blob_id) !== BigInt(entry.anchor.blob_id)
      || record.end_epoch < entry.anchor.end_epoch) throw Error('Foreign archive record');
    if (record.end_epoch > epoch) live.push(entry);
  }
  return live;
}

/** Finite browser-triggered extension; the contract refuses expired archives,
 * protocol horizons, foreign/owner blobs and attached legacy renewal policies. */
export function extendStreamArchiveTx(config: FleetDataConfig, storage: StorageConfig, cap: string, blob: string, epochs: number) {
  validateStreamRetention(0, epochs);
  if (!config.streamRetentionOrigin || !/^0x[0-9a-fA-F]{1,64}$/.test(blob)) throw Error('Choose a verified stream deployment and archive blob object ID');
  const tx = new Transaction();
  tx.moveCall({target:`${storage.packageId}::fleet_storage::extend_stream_blob`,arguments:[tx.object(config.storageId),tx.object(config.budgetId),tx.object(cap),tx.object(storage.walrus.system),tx.pure.address(blob),tx.pure.u32(epochs)]});
  return tx;
}
