import { bcs } from '@mysten/sui/bcs';
import { ObjectError } from '@mysten/sui/client';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { normalizeStructTag, normalizeSuiAddress } from '@mysten/sui/utils';
import { moveType, requireManifest } from './deployment-manifest';
import { deployment_manager_ReceiptDirectoryBcs as Directory, deployment_manager_ReceiptShardBcs as Shard, deployment_manager_ShardedReceiptBcs as Receipt } from './fleet-protocol';
import { batchFields } from './batch-fields';

export function receiptShardIndex(device: string) { return parseInt(normalizeSuiAddress(device).slice(-2), 16) % 16; }
export async function readReceiptDirectory(client: Pick<SuiGrpcClient, 'getDynamicField'>, deployment: string) {
  if (!requireManifest().type_origins['deployment_manager::ReceiptShardKey']) return undefined;
  let dynamicField;
  try { ({ dynamicField } = await client.getDynamicField({ parentId: deployment,
    name: { type: moveType('deployment_manager::ReceiptShardKey'), bcs: new Uint8Array([0]) } })); }
  catch (error) { if (error instanceof ObjectError && error.reason === 'notFound') return undefined; throw error; }
  if (normalizeStructTag(dynamicField.value.type) !== normalizeStructTag(moveType('deployment_manager::ReceiptDirectory'))) throw Error('Unexpected receipt directory type');
  const directory = Directory.parse(dynamicField.value.bcs);
  if (directory.roots.length !== 16 || !['1', '2', '3'].includes(directory.original_schema) || BigInt(directory.migrated) > BigInt(16)) throw Error('Invalid receipt directory');
  return directory;
}
export async function readReceiptPageV3(client: Pick<SuiGrpcClient, 'getObject' | 'getObjects'>, deployment: string, root: string, index: number, start = 0, limit = 64) {
  if (!Number.isSafeInteger(start) || start < 0 || !Number.isInteger(limit) || limit < 1 || limit > 64) throw Error('Invalid receipt page');
  const { object } = await client.getObject({ objectId: root, include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType('deployment_manager::ReceiptShard')) || !('Shared' in object.owner)) throw Error('Unexpected receipt root type or owner');
  const shard = Shard.parse(object.content);
  if (normalizeSuiAddress(shard.id) !== normalizeSuiAddress(root) || normalizeSuiAddress(shard.deployment) !== normalizeSuiAddress(deployment) || shard.index !== String(index) || !shard.migrated) throw Error('Receipt root binding mismatch');
  const count = Number(shard.count);
  if (!Number.isSafeInteger(count) || start > count) throw Error('Invalid receipt count');
  const end = Math.min(start + limit, count);
  const indices = await batchFields(client, Array.from({ length: end - start }, (_, n) => ({ parent: root,
    keyType: moveType('deployment_manager::ReceiptIndexKey'), key: bcs.u64().serialize(start + n).toBytes(), valueType: '0x2::object::ID' })));
  const devices = indices.map(bytes => { if (!bytes) throw Error('Missing receipt index'); const device = bcs.Address.parse(bytes); if (receiptShardIndex(device) !== index) throw Error('Wrong receipt shard'); return device; });
  const receipts = await batchFields(client, devices.map(device => ({ parent: root, keyType: moveType('deployment_manager::ReceiptKey'), key: bcs.Address.serialize(device).toBytes(), valueType: moveType('deployment_manager::ShardedReceipt') })));
  const statuses = Object.fromEntries(receipts.map((bytes, n) => { if (!bytes) throw Error('Missing receipt'); const receipt = Receipt.parse(bytes); if (![1, 2].includes(receipt.status)) throw Error('Invalid receipt status'); return [devices[n], receipt.status]; }));
  return { statuses, next: end, hasMore: end < count };
}
export async function readShardedReceipts(client: Pick<SuiGrpcClient, 'getDynamicField' | 'getObject' | 'getObjects'>, deployment: string) {
  const directory = await readReceiptDirectory(client, deployment);
  if (!directory) throw Error('Missing v3 receipt directory');
  if (!directory.ready || directory.retiring || directory.migrated !== '16') return { verified: false, statuses: {} as Record<string, number> };
  if (new Set(directory.roots).size !== 16) throw Error('Duplicate receipt roots');
  const statuses: Record<string, number> = {};
  for (let index = 0; index < 16; index++) {
    let start = 0;
    for (;;) {
      const page = await readReceiptPageV3(client, deployment, directory.roots[index], index, start);
      Object.assign(statuses, page.statuses);
      if (!page.hasMore) break;
      start = page.next;
    }
  }
  // Retirement/migration must never masquerade as a complete authoritative view.
  const after = await readReceiptDirectory(client, deployment);
  if (!after?.ready || after.retiring) throw Error('Receipt authority changed during read');
  return { verified: true, statuses };
}

/** One resumable operator step, never an unbounded wallet transaction. Repeat
 * after confirmation until the directory is ready, or until cleanup is complete.
 */
export async function buildReceiptMaintenanceTx(client: Pick<SuiGrpcClient, 'getObject' | 'getDynamicField'>, deployment: string, action: 'migrate' | 'cleanup') {
  const { Transaction } = await import('@mysten/sui/transactions');
  const { calls } = await import('./fleet-protocol');
  const manifest = requireManifest();
  if (!manifest.type_origins['deployment_manager::ReceiptShardKey']) throw Error('Manifest does not enable sharded receipt maintenance');
  const tx = new Transaction(), packageId = manifest.call_package_id;
  const { object } = await client.getObject({ objectId: deployment, include: { content: true } });
  const { deployment_manager_DeploymentRecordBcs: Record } = await import('./fleet-protocol');
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType('deployment_manager::DeploymentRecord'))) throw Error('Unexpected deployment type');
  const record = Record.parse(object.content);
  const directory = await readReceiptDirectory(client, deployment);
  if (!directory) {
    if (action !== 'migrate') throw Error('Deployment has no shards');
    if (record.schema_version === '2') calls.component_catalog_begin_sharded_receipts(tx, packageId, deployment);
    else calls.deployment_manager_begin_sharded_receipts(tx, packageId, deployment);
    return tx;
  }
  if (action === 'migrate') {
    if (directory.retiring) throw Error('Deployment is retiring');
    if (directory.ready) throw Error('Receipt migration already complete');
    let complete = 0;
    for (const root of directory.roots) {
      const { object } = await client.getObject({ objectId: root, include: { content: true } });
      const shard = Shard.parse(object.content);
      if (shard.migrated) { complete++; continue; }
      calls.deployment_manager_migrate_receipt_chunk(tx, packageId, deployment, root, 64);
    }
    // A separate finish transaction keeps retries deterministic after chunks.
    if (complete === 16) calls.deployment_manager_finish_receipt_migration(tx, packageId, deployment);
  } else {
    if (!directory.retiring) { calls.deployment_manager_begin_receipt_cleanup(tx, packageId, deployment); return tx; }
    let live = 0;
    for (const root of directory.roots) {
      if (normalizeSuiAddress(root) === normalizeSuiAddress('0x0')) continue;
      live++;
      if (live > 4) continue; // at most 512 deleted fields per PTB
      const { object } = await client.getObject({ objectId: root, include: { content: true } });
      const shard = Shard.parse(object.content);
      if (shard.count === '0') calls.deployment_manager_retire_receipt_shard(tx, packageId, deployment, root);
      else calls.deployment_manager_cleanup_receipt_chunk(tx, packageId, deployment, root, 64);
    }
    if (live === 0) {
      if (directory.original_schema === '2') calls.component_catalog_delete_deployment(tx, packageId, deployment);
      else if (directory.original_schema === '3') calls.deployment_manager_delete_rollout(tx, packageId, deployment);
      else calls.deployment_manager_delete_deployment(tx, packageId, deployment);
    }
  }
  return tx;
}
