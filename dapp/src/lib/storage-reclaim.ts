// SF-129 storage reclaim. Sui refunds most of an object's storage deposit when
// it is deleted; these helpers find fleet objects that are no longer needed,
// build the owner transaction and preview its actual effects before signing.
// The contracts re-check every condition: a stale plan aborts, never deletes
// unexpired data or a record a group still desires.
import { bcs } from "@mysten/sui/bcs";
import { ObjectError } from "@mysten/sui/client";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag, normalizeSuiAddress as id } from "@mysten/sui/utils";
import { calls } from "./fleet-protocol";
import { moveType } from "./deployment-manifest";
import { readDesiredState } from "./desired-state";
import { AnchorBcs, readDeviceData, type FleetDataConfig } from "./fleet-data";
import { RecordBcs } from "./retention";
import type { DeploymentEvent } from "./sui";

/** Per Move call; the contracts refuse larger batches. */
export const RECLAIM_BATCH = 64;
/** Per transaction, so one signature stays well inside protocol limits. */
export const RECLAIM_TRANSACTION_LIMIT = 256;
/** Bounds browser reads per device; larger histories are reclaimed in pages. */
export const RECLAIM_SCAN_LIMIT = 512;

type Anchor = ReturnType<typeof AnchorBcs.parse>;
const notFound = (error: unknown) => error instanceof ObjectError && error.reason === "notFound";
const chunks = <T,>(values: T[]) => Array.from({ length: Math.ceil(values.length / RECLAIM_BATCH) }, (_, i) => values.slice(i * RECLAIM_BATCH, (i + 1) * RECLAIM_BATCH));

// ── Previews ────────────────────────────────────────────────────────────────

export interface ReclaimPreview {
  ok: boolean;
  /** Storage rebate of deleted/shrunk objects. */
  rebateMist: bigint;
  storageCostMist: bigint;
  computationMist: bigint;
  /** SUI returned to the signer after this transaction's own fees; negative is a net cost. */
  netMist: bigint;
  error?: string;
}

/** Net effect of executed gas: what the signer's balance changes by. */
export function netReclaim(gas: { computationCost: string; storageCost: string; storageRebate: string }) {
  const rebateMist = BigInt(gas.storageRebate), storageCostMist = BigInt(gas.storageCost), computationMist = BigInt(gas.computationCost);
  return { rebateMist, storageCostMist, computationMist, netMist: rebateMist - storageCostMist - computationMist };
}

/** Simulate with the sender's own gas so the shown amount is what signing would do. */
export async function previewReclaim(client: Pick<SuiGrpcClient, "simulateTransaction">, transaction: Transaction, sender: string): Promise<ReclaimPreview> {
  transaction.setSenderIfNotSet(sender);
  const result = await client.simulateTransaction({ transaction, include: { effects: true } });
  const executed = result.$kind === "Transaction" ? result.Transaction : result.FailedTransaction;
  if (!executed?.effects) throw new Error("Simulation returned no effects");
  const preview = netReclaim(executed.effects.gasUsed);
  if (result.$kind !== "Transaction" || !executed.effects.status.success) {
    const status = executed.effects.status;
    return { ...preview, ok: false, error: status.success ? "Simulation failed" : JSON.stringify(status.error).slice(0, 300) };
  }
  return { ...preview, ok: true };
}

/** "0.0123 SUI", signed: a positive amount is returned to the wallet. */
export function formatReclaim(mist: bigint) {
  const sign = mist < BigInt(0) ? "-" : "", abs = mist < BigInt(0) ? -mist : mist;
  return `${sign}${(abs / BigInt(1e9)).toString()}.${(abs % BigInt(1e9)).toString().padStart(9, "0")} SUI`;
}

// ── Expired archives ────────────────────────────────────────────────────────

export interface ArchiveCandidate {
  deviceId: string;
  writerId: string;
  index: number;
  anchor: Anchor;
  /** Still held by fleet storage: pruning also releases and burns the blob. */
  held: boolean;
  endEpoch: number;
}

export interface ArchivePlan {
  epoch: number;
  anchors: ArchiveCandidate[];
  /** Expired device blobs that no scanned anchor references. */
  loose: string[];
  /** Live anchors read; unexpired ones are kept. */
  scanned: number;
  /** More history exists past the scan bound; reclaim again for the rest. */
  truncated: boolean;
}

async function heldRecord(client: Pick<SuiGrpcClient, "getDynamicField">, config: FleetDataConfig, storageOrigin: string, blobObjectId: string) {
  try {
    const field = await client.getDynamicField({ parentId: config.storageId, name: {
      type: `${storageOrigin}::fleet_storage::RecordKey`, bcs: bcs.struct("RecordKey", { blob_object_id: bcs.Address }).serialize({ blob_object_id: blobObjectId }).toBytes() } });
    if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(`${storageOrigin}::fleet_storage::BlobRecord`)) throw new Error("Unexpected fleet blob record type");
    return RecordBcs.parse(field.dynamicField.value.bcs);
  } catch (error) {
    if (notFound(error)) return undefined;
    throw error;
  }
}

/**
 * Find expired archives of the given devices. A blob still in fleet storage is
 * expired when its current end epoch (including renewals) has passed; one the
 * owner released earlier is judged by its anchored end epoch, as on chain.
 * Missing indices were already pruned and are skipped.
 */
export async function planArchiveReclaim(client: Pick<SuiGrpcClient, "getObject" | "getDynamicField" | "listDynamicFields" | "ledgerService">,
  config: FleetDataConfig, storageOrigin: string, deviceIds: string[], epoch: number, scanLimit = RECLAIM_SCAN_LIMIT): Promise<ArchivePlan> {
  if (!Number.isSafeInteger(epoch) || epoch < 0) throw new Error("Invalid Walrus epoch");
  const anchors: ArchiveCandidate[] = [], referenced = new Set<string>();
  let scanned = 0, truncated = false;
  for (const deviceId of deviceIds) {
    const writer = await readDeviceData(client, config, deviceId);
    if (!writer) continue;
    const count = Number(writer.anchor_count);
    if (!Number.isSafeInteger(count)) throw new Error("Archive index exceeds browser limits");
    let reads = 0;
    for (let index = 0; index < count; index++) {
      if (reads++ >= scanLimit) { truncated = true; break; }
      let field;
      try {
        field = await client.getDynamicField({ parentId: writer.id, name: {
          type: `${config.typeOrigin}::fleet_data::AnchorKey`, bcs: bcs.struct("AnchorKey", { index: bcs.u64() }).serialize({ index }).toBytes() } });
      } catch (error) {
        if (notFound(error)) continue;
        throw error;
      }
      if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(`${config.typeOrigin}::fleet_data::Anchor`)) throw new Error("Invalid archive index type");
      const anchor = AnchorBcs.parse(field.dynamicField.value.bcs), blob = id(anchor.blob_object_id);
      scanned++;
      referenced.add(blob);
      const record = await heldRecord(client, config, storageOrigin, blob);
      const endEpoch = record ? record.end_epoch : anchor.end_epoch;
      if (endEpoch <= epoch) anchors.push({ deviceId: id(deviceId), writerId: id(writer.id), index, anchor, held: !!record, endEpoch });
    }
  }
  const loose: string[] = [];
  let cursor: string | null | undefined;
  do {
    const page = await client.listDynamicFields({ parentId: config.storageId, cursor, limit: 100, include: { value: true } });
    for (const entry of page.dynamicFields) {
      if (normalizeStructTag(entry.name.type) !== normalizeStructTag(`${storageOrigin}::fleet_storage::RecordKey`)) continue;
      const blob = id(bcs.Address.parse(entry.name.bcs)), record = RecordBcs.parse(entry.value.bcs);
      // Device archives only: owner-registered release and recipe blobs stay
      // explicit owner decisions on the storage panel.
      if (record.device_id && record.end_epoch <= epoch && !referenced.has(blob)) loose.push(blob);
    }
    cursor = page.hasNextPage ? page.cursor : undefined;
  } while (cursor && loose.length < RECLAIM_TRANSACTION_LIMIT);
  return { epoch, anchors, loose, scanned, truncated };
}

/** The first transaction's worth of a plan; reclaim again for the remainder. */
export function buildArchiveReclaimTx(config: FleetDataConfig, budgetCapId: string, walrusSystem: string, plan: ArchivePlan): { tx: Transaction; anchors: number; loose: number } {
  const tx = new Transaction();
  const byWriter = new Map<string, number[]>();
  let anchors = 0;
  for (const c of plan.anchors) {
    if (anchors >= RECLAIM_TRANSACTION_LIMIT) break;
    byWriter.set(c.writerId, [...(byWriter.get(c.writerId) ?? []), c.index]);
    anchors++;
  }
  for (const [writer, indices] of byWriter) for (const batch of chunks(indices)) {
    tx.moveCall({ target: `${config.packageId}::fleet_data::prune_expired_anchors`, arguments: [tx.object(config.fleetId), tx.object(writer),
      tx.object(config.budgetId), tx.object(budgetCapId), tx.object(config.storageId), tx.object(walrusSystem), tx.pure.vector("u64", batch)] });
  }
  const loose = plan.loose.slice(0, Math.max(0, RECLAIM_TRANSACTION_LIMIT - anchors));
  for (const batch of chunks(loose)) {
    tx.moveCall({ target: `${config.packageId}::fleet_data::burn_expired_blobs`, arguments: [tx.object(config.fleetId), tx.object(config.budgetId),
      tx.object(budgetCapId), tx.object(config.storageId), tx.object(walrusSystem), tx.pure.vector("address", batch)] });
  }
  if (anchors + loose.length === 0) throw new Error("Nothing to reclaim");
  return { tx, anchors, loose: loose.length };
}

// ── Finished deployment records ─────────────────────────────────────────────

export interface DeploymentReclaim {
  deployment: DeploymentEvent;
  /** Target groups a managed record must be checked against on chain. */
  groups: string[];
  /** Why the record counts as finished, or why it is kept. */
  reason: string;
  eligible: boolean;
}

async function isManaged(client: Pick<SuiGrpcClient, "getDynamicField">, deploymentId: string) {
  try {
    await client.getDynamicField({ parentId: deploymentId, name: { type: moveType("deployment_manager::ManagedKey"), bcs: bcs.bool().serialize(false).toBytes() } });
    return true;
  } catch (error) {
    if (notFound(error)) return false;
    throw error;
  }
}

/** Finished: cancelled, past its expiry, or every resolved target holds a verified receipt. */
export function finishedReason(d: DeploymentEvent, nowMs: number): string | undefined {
  if (d.control.state === 2) return "cancelled";
  const expires = Number(d.control.expires_ms);
  if (expires > 0 && nowMs >= expires) return "expired";
  if (d.receiptsVerified && d.resolvedDeviceIds.length > 0 && d.resolvedDeviceIds.every((device) => d.statuses[device] === 1 || d.statuses[device] === 2)) return "every target reported";
  return undefined;
}

/**
 * Classify the account's own records. A record any target group still
 * desires is kept; it is reclaimable after a revision supersedes it.
 */
export async function planDeploymentReclaim(client: Pick<SuiGrpcClient, "getDynamicField" | "getObject">, deployments: DeploymentEvent[], account: string, nowMs = Date.now()): Promise<DeploymentReclaim[]> {
  const out: DeploymentReclaim[] = [];
  for (const d of deployments.filter((d) => id(d.creator) === id(account))) {
    const finished = finishedReason(d, nowMs);
    if (!finished) { out.push({ deployment: d, groups: [], reason: "still in progress", eligible: false }); continue; }
    const managed = await isManaged(client, d.deploymentId);
    const groups = managed ? [...new Set(d.targetGroups.map((g) => id(g)))] : [];
    let blocked: string | undefined;
    for (const group of groups) {
      try {
        await client.getObject({ objectId: group });
      } catch (error) {
        if (!notFound(error)) throw error;
        blocked = "a target group was deleted, so the on-chain check cannot run; delete it individually after review";
        break;
      }
      const state = await readDesiredState(client, group);
      if (state && Object.values(state.components).some((value) => id(value) === id(d.deploymentId))) { blocked = "still desired by a group; publish a revision that supersedes it first"; break; }
    }
    out.push(blocked ? { deployment: d, groups, reason: blocked, eligible: false } : { deployment: d, groups, reason: finished, eligible: true });
  }
  return out;
}

/** One transaction retiring every eligible record through the checked path. */
export function buildRetireDeploymentsTx(packageId: string, records: DeploymentReclaim[]): Transaction {
  const eligible = records.filter((r) => r.eligible);
  if (eligible.length === 0) throw new Error("Nothing to reclaim");
  if (eligible.length > RECLAIM_BATCH) throw new Error(`Reclaim at most ${RECLAIM_BATCH} records per transaction`);
  const tx = new Transaction();
  for (const r of eligible) {
    const [retirement] = calls.desired_state_begin_retirement(tx, packageId, r.deployment.deploymentId);
    for (const group of r.groups) calls.desired_state_confirm_not_desired(tx, packageId, retirement, group);
    calls.desired_state_retire(tx, packageId, retirement, r.deployment.deploymentId);
  }
  return tx;
}
