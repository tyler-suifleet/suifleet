import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction } from '@mysten/sui/transactions';
import type { FleetStorageTarget } from './fleet-upload';
import { NETWORK } from './constants';

export const DAY = 86_400_000;
export interface StorageSchedule { epoch: number; epochDurationMs: number; firstEpochStartMs: number; maxEpochs: number }
export function periodEpochs(days: number, schedule: StorageSchedule) {
  if (!Number.isSafeInteger(schedule.epoch) || schedule.epoch < 0 || !Number.isSafeInteger(schedule.maxEpochs) || schedule.maxEpochs < 1 || !Number.isSafeInteger(schedule.firstEpochStartMs) || schedule.firstEpochStartMs < 0) throw Error('Walrus storage schedule unavailable');
  if (!Number.isFinite(days) || days <= 0 || !Number.isSafeInteger(schedule.epochDurationMs) || schedule.epochDurationMs <= 0) throw Error('Choose a positive storage period in days');
  const epochs = Math.ceil(days * DAY / schedule.epochDurationMs);
  if (!Number.isSafeInteger(epochs) || epochs < 1 || epochs > schedule.maxEpochs) throw Error(`Storage period exceeds the network limit of ${schedule.maxEpochs} epochs`);
  return epochs;
}
export function expiryTime(endEpoch: number, schedule: StorageSchedule) {
  const ms = schedule.firstEpochStartMs + endEpoch * schedule.epochDurationMs;
  if (!Number.isSafeInteger(endEpoch) || endEpoch < 0 || !Number.isSafeInteger(ms) || ms < 0) throw Error('Invalid storage expiry');
  return ms;
}
export function expiryWarning(endMs: number | undefined, now = Date.now()) {
  if (endMs === undefined) return 'Storage expiry unknown; future downloads are not guaranteed.';
  if (endMs <= now) return 'Storage expired. Offline devices cannot download this release; restore verified bytes before deploying.';
  if (endMs - now <= 7 * DAY) return 'Storage expires within 7 days. Extend it so offline and newly targeted devices can download this release.';
  return undefined;
}
export async function releaseWalrus(client: SuiGrpcClient) {
  if (NETWORK !== 'mainnet' && NETWORK !== 'testnet') throw Error('Walrus storage requires a configured supported network');
  const { walrus, MAINNET_WALRUS_PACKAGE_CONFIG, TESTNET_WALRUS_PACKAGE_CONFIG } = await import('@mysten/walrus');
  return client.$extend(walrus({ packageConfig: NETWORK === 'mainnet' ? MAINNET_WALRUS_PACKAGE_CONFIG : TESTNET_WALRUS_PACKAGE_CONFIG })).walrus;
}
export async function storageSchedule(client: SuiGrpcClient): Promise<StorageSchedule> {
  const w = await releaseWalrus(client);
  const [state, staking] = await Promise.all([w.systemState(), w.stakingState()]);
  const schedule = { epoch: state.committee.epoch, epochDurationMs: Number(staking.epoch_duration), firstEpochStartMs: Number(staking.first_epoch_start), maxEpochs: state.future_accounting.length - 1 };
  periodEpochs(schedule.epochDurationMs / DAY, schedule);
  return schedule;
}
export function buildExtendFleetReleaseTx(target: FleetStorageTarget, blob: string, epochs: number) {
  if (!Number.isSafeInteger(epochs) || epochs <= 0 || epochs > 0xffffffff) throw Error('Choose a positive extension');
  const tx = new Transaction();
  tx.moveCall({ target: `${target.config.packageId}::fleet_storage::extend_blob`, arguments: [tx.object(target.storageId), tx.object(target.budgetId), tx.object(target.capId), tx.object(target.config.walrus.system), tx.pure.id(blob), tx.pure.u32(epochs)] });
  return tx;
}
export async function buildExtendWalletReleaseTx(client: SuiGrpcClient, blob: string, epochs: number) {
  const schedule = await storageSchedule(client);
  if (!Number.isSafeInteger(epochs) || epochs < 1 || epochs > schedule.maxEpochs) throw Error('Invalid extension period');
  return (await releaseWalrus(client)).extendBlobTransaction({ blobObjectId: blob, epochs });
}

/** Current owned objects, never browser receipts, determine which blobs can be extended. */
export async function releaseObjects(client: SuiGrpcClient, owner: string, fleet?: FleetStorageTarget) {
  const w = await releaseWalrus(client);
  const { blobIdFromInt } = await import('@mysten/walrus');
  const objects: { blobId: string; id: string; endEpoch: number; size: number; fleet: boolean }[] = [];
  let cursor: string | null | undefined;
  do {
    const page = await client.listOwnedObjects({ owner, type: await w.getBlobType(), limit: 50, cursor });
    const blobs = await Promise.all(page.objects.map(o => w.getBlobObject(o.objectId)));
    objects.push(...blobs.map(b => ({ blobId: blobIdFromInt(BigInt(b.blob_id)), id: b.id, endEpoch: b.storage.end_epoch, size: Number(b.size), fleet: false })));
    if (page.hasNextPage && (!page.cursor || page.cursor === cursor)) throw Error('Storage pagination made no progress');
    cursor = page.hasNextPage ? page.cursor : undefined;
  } while (cursor);
  if (fleet) {
    const { RecordBcs } = await import('./retention');
    const { bcs } = await import('@mysten/sui/bcs');
    const { normalizeStructTag } = await import('@mysten/sui/utils');
    do {
      const page = await client.listDynamicFields({ parentId: fleet.storageId, limit: 100, cursor, include: { value: true } });
      for (const field of page.dynamicFields) {
        if (normalizeStructTag(field.name.type) !== normalizeStructTag(`${fleet.config.storageOrigin ?? fleet.config.packageId}::fleet_storage::RecordKey`)) continue;
        if (normalizeStructTag(field.value.type) !== normalizeStructTag(`${fleet.config.storageOrigin ?? fleet.config.packageId}::fleet_storage::BlobRecord`)) throw Error('Unexpected fleet blob record');
        const record = RecordBcs.parse(field.value.bcs);
        objects.push({ blobId: blobIdFromInt(BigInt(record.blob_id)), id: bcs.Address.parse(field.name.bcs), endEpoch: record.end_epoch, size: Number(record.size), fleet: true });
      }
      if (page.hasNextPage && (!page.cursor || page.cursor === cursor)) throw Error('Fleet storage pagination made no progress');
      cursor = page.hasNextPage ? page.cursor : undefined;
    } while (cursor);
  }
  return objects;
}
