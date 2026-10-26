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
import { readReceiptDirectory } from "./receipt-shards";
import { AnchorBcs, readDeviceData, type FleetDataConfig } from "./fleet-data";
import { RecordBcs } from "./retention";
import type { DeploymentEvent } from "./sui";

/** Per Move call; the contracts refuse larger batches. */
export const RECLAIM_BATCH = 64;
/** Per transaction, so one signature stays well inside protocol limits. */
export const RECLAIM_TRANSACTION_LIMIT = 256;
/** SF-139 opaque items per anchor; pruning an anchor also deletes them. */
export const MAX_ANCHOR_ITEMS = 16;
/** Bounds dynamic fields listed per device and scan (about two per live
 * archive); larger histories continue from the returned cursor. */
export const RECLAIM_SCAN_LIMIT = 1024;

type Anchor = ReturnType<typeof AnchorBcs.parse>;
const notFound = (error: unknown) => error instanceof ObjectError && error.reason === "notFound";
const listFields = (client: Pick<SuiGrpcClient, "listDynamicFields">, parentId: string, cursor: string | null | undefined, limit: number) =>
  client.listDynamicFields({ parentId, cursor, limit, include: { value: true } });
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

/** Build with the sender's real gas coins, then simulate those exact bytes:
 * an unbuilt simulation uses a mocked gas coin and misstates the rebate. */
export async function previewReclaim(client: SuiGrpcClient, transaction: Transaction, sender: string): Promise<ReclaimPreview> {
  transaction.setSenderIfNotSet(sender);
  const zero = BigInt(0), refused = (error: unknown): ReclaimPreview => ({ ok: false, rebateMist: zero, storageCostMist: zero, computationMist: zero, netMist: zero,
    error: (error instanceof Error ? error.message : String(error)).slice(0, 300) });
  let bytes: Uint8Array;
  // Gas estimation runs the transaction; an abort (for example an archive
  // that is not expired yet) is a refusal before anything is signed.
  try { bytes = await transaction.build({ client }); } catch (error) { return refused(error); }
  const result = await client.simulateTransaction({ transaction: bytes, include: { effects: true } });
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
  /** SF-139 opaque item indexes deleted with the anchor; weighs its batch. */
  items: number;
}

export interface ArchivePlan {
  epoch: number;
  anchors: ArchiveCandidate[];
  /** Expired device blobs that no scanned anchor references. */
  loose: string[];
  /** Live anchors read; unexpired ones are kept. */
  scanned: number;
  /** More history exists past the scan bound; continue from `resume`. */
  truncated: boolean;
  /** Per device, the listing cursor where the bounded scan stopped. */
  resume: Record<string, string>;
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

const ItemKeyBcs = bcs.struct("ItemKey", { batch: bcs.u64(), item: bcs.u64() });

/** Verified origin of the SF-139 item index types; absent before that upgrade. */
async function itemOrigin(client: Pick<SuiGrpcClient, "getObject">, config: FleetDataConfig) {
  const { object } = await client.getObject({ objectId: config.packageId, include: { objectBcs: true } });
  const origins = bcs.Object.parse(object.objectBcs).data.Package?.typeOriginTable ?? [];
  const [key, value] = ["ItemKey", "DataItem"].map((name) => origins.filter((e) => e.moduleName === "fleet_data" && e.datatypeName === name));
  if (!key.length && !value.length) return undefined;
  if (key.length !== 1 || value.length !== 1 || id(key[0].package) !== id(value[0].package)) throw new Error("Opaque index origin mismatch");
  return id(key[0].package);
}

/** Items are numbered contiguously from 0, as `anchor_items` writes them. */
async function countItems(client: Pick<SuiGrpcClient, "getDynamicField">, writerId: string, origin: string, batch: string) {
  let item = 0;
  for (; item < MAX_ANCHOR_ITEMS; item++) {
    try {
      await client.getDynamicField({ parentId: writerId, name: { type: `${origin}::fleet_data::ItemKey`, bcs: ItemKeyBcs.serialize({ batch, item }).toBytes() } });
    } catch (error) {
      if (notFound(error)) break;
      throw error;
    }
  }
  return item;
}

/**
 * Find expired archives of the given devices. A blob still in fleet storage is
 * expired when its current end epoch (including renewals) has passed; one the
 * owner released earlier is judged by its anchored end epoch, as on chain.
 * Only existing index entries are listed, so pruned history costs no reads and
 * never blocks the scan. Each device lists at most `scanLimit` fields; pass the
 * returned `resume` back to continue past that bound.
 */
export async function planArchiveReclaim(client: Pick<SuiGrpcClient, "getObject" | "getDynamicField" | "listDynamicFields" | "ledgerService">,
  config: FleetDataConfig, storageOrigin: string, deviceIds: string[], epoch: number, scanLimit = RECLAIM_SCAN_LIMIT,
  from: Record<string, string> = {}): Promise<ArchivePlan> {
  if (!Number.isSafeInteger(epoch) || epoch < 0) throw new Error("Invalid Walrus epoch");
  if (!Number.isSafeInteger(scanLimit) || scanLimit < 1) throw new Error("Invalid scan bound");
  const anchors: ArchiveCandidate[] = [], referenced = new Set<string>(), resume: Record<string, string> = {};
  const anchorKey = normalizeStructTag(`${config.typeOrigin}::fleet_data::AnchorKey`), anchorType = normalizeStructTag(`${config.typeOrigin}::fleet_data::Anchor`);
  const opaque = await itemOrigin(client, config), itemKey = opaque && normalizeStructTag(`${opaque}::fleet_data::ItemKey`);
  let scanned = 0;
  for (const deviceId of deviceIds) {
    const writer = await readDeviceData(client, config, deviceId);
    if (!writer) continue;
    const count = BigInt(writer.anchor_count);
    let cursor: string | null | undefined = from[id(deviceId)], listed = 0, more = true;
    const found: ArchiveCandidate[] = [], items = new Map<string, number>(), resumed = !!cursor;
    while (more && listed < scanLimit) {
      const page = await listFields(client, writer.id, cursor, Math.min(100, scanLimit - listed));
      listed += page.dynamicFields.length;
      more = page.hasNextPage && !!page.cursor;
      cursor = page.cursor;
      for (const entry of page.dynamicFields) {
        // Reuse markers and diagnostic requests share the writer's fields.
        if (itemKey && normalizeStructTag(entry.name.type) === itemKey) {
          const { batch } = ItemKeyBcs.parse(entry.name.bcs);
          items.set(batch, (items.get(batch) ?? 0) + 1);
          continue;
        }
        if (normalizeStructTag(entry.name.type) !== anchorKey) continue;
        if (normalizeStructTag(entry.valueType) !== anchorType) throw new Error("Invalid archive index type");
        const index = BigInt(bcs.u64().parse(entry.name.bcs));
        if (index >= count) throw new Error("Archive index beyond the anchor count");
        const anchor = AnchorBcs.parse(entry.value.bcs), blob = id(anchor.blob_object_id);
        scanned++;
        referenced.add(blob);
        // Renewal only extends a held blob past its anchored end, so a later
        // anchored end is unexpired either way and needs no record read.
        if (anchor.end_epoch > epoch) continue;
        const record = await heldRecord(client, config, storageOrigin, blob);
        const endEpoch = record ? record.end_epoch : anchor.end_epoch;
        if (endEpoch <= epoch) found.push({ deviceId: id(deviceId), writerId: id(writer.id), index: Number(index), anchor, held: !!record, endEpoch, items: 0 });
      }
    }
    // A complete listing counts every item; a bounded or resumed one may have
    // missed some, so those candidates are counted exactly before batching.
    for (const c of found) c.items = opaque && (more || resumed) ? await countItems(client, c.writerId, opaque, c.anchor.seq) : items.get(c.anchor.seq) ?? 0;
    anchors.push(...found);
    if (more && cursor) resume[id(deviceId)] = cursor;
  }
  // Listing order is by field ID; keep each writer's batches in index order.
  anchors.sort((x, y) => x.writerId.localeCompare(y.writerId) || x.index - y.index);
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
  return { epoch, anchors, loose, scanned, truncated: Object.keys(resume).length > 0, resume };
}

/** The first transaction's worth of a plan; reclaim again for the remainder. */
export function buildArchiveReclaimTx(config: FleetDataConfig, budgetCapId: string, walrusSystem: string, plan: ArchivePlan): { tx: Transaction; anchors: number; loose: number } {
  const tx = new Transaction();
  const byWriter = new Map<string, number[]>();
  // Each anchor's item indexes are deleted with it, so they count toward the
  // per-transaction bound; an oversized first anchor still goes alone.
  let anchors = 0, weight = 0;
  for (const c of plan.anchors) {
    // Anchor, blob reuse marker and at most one compact-head field.
    const cost = 3 + c.items;
    if (anchors > 0 && weight + cost > RECLAIM_TRANSACTION_LIMIT) break;
    byWriter.set(c.writerId, [...(byWriter.get(c.writerId) ?? []), c.index]);
    anchors++; weight += cost;
  }
  for (const [writer, indices] of byWriter) for (const batch of chunks(indices)) {
    tx.moveCall({ target: `${config.packageId}::fleet_data::prune_expired_anchors`, arguments: [tx.object(config.fleetId), tx.object(writer),
      tx.object(config.budgetId), tx.object(budgetCapId), tx.object(config.storageId), tx.object(walrusSystem), tx.pure.vector("u64", batch)] });
  }
  const loose = plan.loose.slice(0, Math.max(0, RECLAIM_TRANSACTION_LIMIT - weight));
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
    // SF-127 sharded receipts: the record is deletable only after receipt
    // cleanup has retired every root; `retire` aborts before that.
    const directory = await readReceiptDirectory(client, d.deploymentId);
    if (directory && !(directory.retiring && directory.remaining === "0")) {
      out.push({ deployment: d, groups: [], reason: "sharded receipts; finish receipt cleanup on the deployment first", eligible: false });
      continue;
    }
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
