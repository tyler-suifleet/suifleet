import { bcs } from '@mysten/sui/bcs';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction, coinWithBalance } from '@mysten/sui/transactions';
import { normalizeStructTag, normalizeSuiAddress } from '@mysten/sui/utils';
import type { StorageConfig } from './fleet-budget';

const Balance = bcs.struct('Balance', { value: bcs.u64() });
export const RetentionBcs = bcs.struct('Retention', {
  executor: bcs.Address, margin: bcs.u32(), wal: Balance, gas: Balance,
  wal_per_epoch: bcs.u64(), gas_per_epoch: bcs.u64(), reward: bcs.u64(),
  epoch: bcs.u32(), wal_spent: bcs.u64(), gas_spent: bcs.u64(), revision: bcs.u64(),
  pending_wal: bcs.u64(), pending_count: bcs.u64(), covered_count: bcs.u64(),
});
export const RenewalBcs = bcs.struct('Renewal', { target: bcs.u32(), max_fee: bcs.u64(), last_success_epoch: bcs.option(bcs.u32()) });
export const RecordBcs = bcs.struct('BlobRecord', { device_id: bcs.option(bcs.Address), blob_id: bcs.u256(), size: bcs.u64(),
  encoded_size: bcs.u64(), end_epoch: bcs.u32(), uploaded_at_ms: bcs.u64() });
export type RetentionView = ReturnType<typeof RetentionBcs.parse>;
export interface RetainedBlob { id: string; end: number; encodedSize: bigint; policy?: ReturnType<typeof RenewalBcs.parse> }

/** Origins are explicit after upgrades: existing records retain v1 origins. */
export function retentionOrigin(): string | undefined {
  const value = process.env.NEXT_PUBLIC_FLEET_RETENTION_ORIGIN;
  if (!value) return undefined;
  return normalizeSuiAddress(value);
}
export async function fetchRetention(client: SuiGrpcClient, storage: string, origin: string, storageOrigin: string) {
  let cursor: string | null | undefined;
  let policy: RetentionView | undefined;
  const records = new Map<string, RetainedBlob>();
  const renewals = new Map<string, ReturnType<typeof RenewalBcs.parse>>();
  do {
    const page = await client.listDynamicFields({ parentId: storage, cursor, limit: 100, include: { value: true } });
    for (const entry of page.dynamicFields) {
      const type = normalizeStructTag(entry.name.type);
      const matches = (name: string, packageId = origin) => type === normalizeStructTag(`${packageId}::fleet_storage::${name}`);
      if (matches('RetentionKey')) policy = RetentionBcs.parse(entry.value.bcs);
      if (matches('RecordKey', storageOrigin)) {
        const id = bcs.Address.parse(entry.name.bcs), record = RecordBcs.parse(entry.value.bcs);
        records.set(id, { id, end: record.end_epoch, encodedSize: BigInt(record.encoded_size) });
      }
      if (matches('RenewalKey')) renewals.set(bcs.Address.parse(entry.name.bcs), RenewalBcs.parse(entry.value.bcs));
      if (records.size > 1024 || renewals.size > 1024) throw new Error('Retention view exceeds 1024 blobs; export and partition the fleet');
    }
    cursor = page.hasNextPage ? page.cursor : undefined;
  } while (cursor);
  const blobs = [...records.values()].map((record) => ({ ...record, policy: renewals.get(record.id) }));
  return { policy, blobs };
}

export function retentionFacts(policy: RetentionView | undefined, blobs: RetainedBlob[], epoch: number | undefined) {
  if (!policy) return ['Retention is not configured; no scheduled renewal or alerts are guaranteed.'];
  const facts: string[] = [];
  if (BigInt(policy.wal.value) < BigInt(policy.pending_wal) || BigInt(policy.gas.value) < BigInt(policy.pending_count) * BigInt(policy.reward)) facts.push('Protected reserve is underfunded; discretionary writes pause.');
  if (blobs.some((b) => !b.policy)) facts.push('Fleet blob closure is incomplete; discretionary writes pause until every fleet blob has a finite target.');
  if (epoch !== undefined && blobs.some((b) => b.end <= epoch)) facts.push('Blob expired: restore verified customer bytes and authorize an updated reference. Sui cannot reconstruct binaries.');
  if (epoch !== undefined && blobs.some((b) => b.end > epoch && b.end - epoch <= policy.margin)) facts.push('Renewal deadline approaching; check the owner-run worker and its persistent failure/delivery status.');
  facts.push('Targets and reserves are finite. Quote increases, executor outages and withdrawals can put retention at risk.');
  return facts;
}

export function buildMigrateRetentionTx(c: StorageConfig, storage: string, budget: string, cap: string) {
  const tx = new Transaction();
  tx.moveCall({ target: `${c.packageId}::fleet_storage::migrate`, arguments: [tx.object(storage), tx.object(budget), tx.object(cap)] });
  return tx;
}
export function buildConfigureRetentionTx(c: StorageConfig, storage: string, budget: string, cap: string,
  executor: string, margin: number, walLimit: bigint, gasLimit: bigint, reward: bigint) {
  const tx = new Transaction();
  tx.moveCall({ target: `${c.packageId}::fleet_storage::configure_retention`, arguments: [tx.object(storage), tx.object(budget), tx.object(cap),
    tx.pure.address(executor), tx.pure.u32(margin), tx.pure.u64(walLimit), tx.pure.u64(gasLimit), tx.pure.u64(reward)] });
  return tx;
}
export function buildAuthorizeRenewalTx(c: StorageConfig, storage: string, budget: string, cap: string, blob: string, target: number, maxFee: bigint) {
  const tx = new Transaction();
  tx.moveCall({ target: `${c.packageId}::fleet_storage::authorize_renewal`, arguments: [tx.object(storage), tx.object(budget), tx.object(cap),
    tx.pure.id(blob), tx.pure.u32(target), tx.pure.u64(maxFee)] });
  return tx;
}
export function buildFundRetentionTx(c: StorageConfig, storage: string, wal: bigint, gas: bigint) {
  const tx = new Transaction();
  const walCoin = tx.add(coinWithBalance({ type: c.walrus.walType, balance: wal }));
  const [gasCoin] = tx.splitCoins(tx.gas, [tx.pure.u64(gas)]);
  tx.moveCall({ target: `${c.packageId}::fleet_storage::fund_retention`, arguments: [tx.object(storage), walCoin, gasCoin] });
  return tx;
}
export function buildWithdrawRetentionTx(c: StorageConfig, storage: string, budget: string, cap: string, wal: bigint, gas: bigint, sender: string) {
  const tx = new Transaction();
  const [w, g] = tx.moveCall({ target: `${c.packageId}::fleet_storage::withdraw_retention`, arguments: [tx.object(storage), tx.object(budget), tx.object(cap), tx.pure.u64(wal), tx.pure.u64(gas)] });
  tx.transferObjects([w, g], tx.pure.address(sender));
  return tx;
}
