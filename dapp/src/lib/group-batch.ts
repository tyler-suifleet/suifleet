// Bulk group membership (SF-171): select devices, review the exact changes and
// approvals, then apply them in bounded transactions. A failed transaction
// stops the run; nothing is retried automatically.
import { Transaction } from "@mysten/sui/transactions";
import { normalizeSuiAddress } from "@mysten/sui/utils";
import { calls } from "./fleet-protocol";
import { GROUP_BATCH_TYPES } from "./protocol-version";
import { PACKAGE_ID, REGISTRY_ID } from "./constants";
import { appendAddDeviceToGroup, appendRemoveDeviceFromGroup, type DeviceGroup, type DeviceObject } from "./sui";
import type { NamedFleet } from "./customer-fleets";
import { MANIFEST, type DeploymentManifest } from "./deployment-manifest";

export type MembershipAction = "add" | "remove";

/** Matches MAX_GROUP_BATCH in customer.move: changes per managed-fleet transaction. */
export const MAX_FLEET_CHANGES = 100;
/** Wallet-administered groups borrow each device (and may create its inbox). */
export const MAX_WALLET_CHANGES = 25;

/** Whether the deployment has the batch entry points (`customer::add_many_to_group`). */
export function groupBatchSupported(m: DeploymentManifest | undefined = MANIFEST): boolean {
  return !!m && GROUP_BATCH_TYPES.every((name) => name in m.type_origins);
}

const id = (value: string) => normalizeSuiAddress(value);
const fold = (value: string) => value.trim().toLowerCase();

/** Devices whose name, serial, architecture, group or object ID contain the query. */
export function filterDevices(devices: DeviceObject[], query: string, groups: DeviceGroup[] = []): DeviceObject[] {
  const q = fold(query);
  if (!q) return devices;
  return devices.filter((d) => [d.name, d.chainName, d.serial ?? "", d.arch, d.objectId,
    ...groups.filter((g) => g.deviceIds.includes(d.objectId)).map((g) => g.name)].some((field) => fold(field).includes(q)));
}

export interface ListResolution {
  matched: DeviceObject[];
  /** Entries that match no device. */
  unmatched: string[];
  /** Entries that match several devices; never guessed. */
  ambiguous: string[];
}

/**
 * Resolve a pasted list of serials, names or object IDs (one per line, or
 * separated by commas, semicolons or tabs). Exact, case-insensitive matches
 * only: object ID, then serial, then name.
 */
export function resolveDeviceList(text: string, devices: DeviceObject[]): ListResolution {
  const entries = [...new Set(text.split(/[\n\r,;\t]+/).map((entry) => entry.trim()).filter(Boolean))];
  const matched = new Map<string, DeviceObject>(), unmatched: string[] = [], ambiguous: string[] = [];
  for (const entry of entries) {
    const key = fold(entry);
    let found: DeviceObject[] = [];
    if (/^0x[0-9a-f]{1,64}$/.test(key)) found = devices.filter((d) => d.objectId === id(key));
    if (!found.length) found = devices.filter((d) => d.serial !== undefined && fold(d.serial) === key);
    if (!found.length) found = devices.filter((d) => fold(d.name) === key || fold(d.chainName) === key);
    if (found.length === 1) matched.set(found[0].objectId, found[0]);
    else (found.length ? ambiguous : unmatched).push(entry);
  }
  return { matched: [...matched.values()], unmatched, ambiguous };
}

export interface MembershipChange { groupId: string; deviceId: string; arch: string }
export interface SkippedChange { groupId: string; deviceId: string; reason: string }
export interface ArchMismatch { groupId: string; deviceId: string; arch: string; groupArchs: string[] }
export interface MembershipPlan {
  action: MembershipAction;
  changes: MembershipChange[];
  skipped: SkippedChange[];
  /** Added devices whose architecture no current member of the group has. */
  mismatches: ArchMismatch[];
  /** Each entry is one transaction, so one wallet approval. */
  transactions: MembershipChange[][];
}

/**
 * The exact membership changes for `deviceIds` x `groupIds`, grouped into
 * bounded transactions. Devices already in the requested state are skipped.
 * Architecture mismatches are excluded unless `includeMismatched`.
 */
export function planMembership(action: MembershipAction, deviceIds: string[], groupIds: string[],
  devices: DeviceObject[], groups: DeviceGroup[], options: { managed: boolean; includeMismatched?: boolean }): MembershipPlan {
  const byId = new Map(devices.map((d) => [d.objectId, d]));
  const changes: MembershipChange[] = [], skipped: SkippedChange[] = [], mismatches: ArchMismatch[] = [];
  for (const groupId of [...new Set(groupIds)]) {
    const group = groups.find((g) => g.objectId === groupId);
    if (!group) throw Error("Selected group is not in this fleet");
    const members = new Set(group.deviceIds);
    const groupArchs = [...new Set(group.deviceIds.flatMap((m) => byId.get(m)?.arch ?? []))].sort();
    for (const deviceId of [...new Set(deviceIds)]) {
      const device = byId.get(deviceId);
      if (!device) throw Error("Selected device is not in this fleet");
      if (action === "add") {
        if (members.has(deviceId)) { skipped.push({ groupId, deviceId, reason: "already a member" }); continue; }
        if (groupArchs.length && !groupArchs.includes(device.arch)) {
          mismatches.push({ groupId, deviceId, arch: device.arch, groupArchs });
          if (!options.includeMismatched) { skipped.push({ groupId, deviceId, reason: `architecture ${device.arch} differs from members (${groupArchs.join(", ")})` }); continue; }
        }
      } else if (!members.has(deviceId)) { skipped.push({ groupId, deviceId, reason: "not a member" }); continue; }
      changes.push({ groupId, deviceId, arch: device.arch });
    }
  }
  // Consecutive changes of one group and architecture share one batch call.
  const order = new Map(groupIds.map((g, i) => [g, i]));
  changes.sort((a, b) => order.get(a.groupId)! - order.get(b.groupId)! || a.arch.localeCompare(b.arch));
  return { action, changes, skipped, mismatches, transactions: chunk(changes, options.managed) };
}

function chunk(changes: MembershipChange[], managed: boolean): MembershipChange[][] {
  const limit = managed ? MAX_FLEET_CHANGES : MAX_WALLET_CHANGES, out: MembershipChange[][] = [];
  for (const change of changes) {
    const last = out[out.length - 1];
    // A device creates at most one inbox per transaction: a shared object
    // created in a transaction cannot be used again by ID in it.
    if (!last || last.length >= limit || (!managed && last.some((c) => c.deviceId === change.deviceId))) out.push([change]);
    else last.push(change);
  }
  return out;
}

/**
 * One transaction of a plan. Managed fleets with the batch entry points use
 * one bounded call per group and architecture, which the chain re-checks;
 * older deployments use one call per change. `inboxes` lists devices whose
 * inbox exists (wallet-administered groups only), read just before building.
 */
export function buildMembershipTx(action: MembershipAction, changes: MembershipChange[], devices: DeviceObject[],
  fleet?: NamedFleet, inboxes?: Set<string>, batch = groupBatchSupported()): Transaction {
  if (!changes.length || changes.length > (fleet ? MAX_FLEET_CHANGES : MAX_WALLET_CHANGES)) throw Error("Batch size out of bounds");
  const tx = new Transaction();
  if (fleet && batch) {
    let i = 0;
    while (i < changes.length) {
      const { groupId, arch } = changes[i], ids: string[] = [];
      while (i < changes.length && changes[i].groupId === groupId && (action === "remove" || changes[i].arch === arch)) ids.push(changes[i++].deviceId);
      if (action === "add") calls.customer_add_many_to_group(tx, PACKAGE_ID, fleet.customerId, fleet.id, groupId, REGISTRY_ID, ids, arch);
      else calls.customer_remove_many_from_group(tx, PACKAGE_ID, fleet.customerId, fleet.id, groupId, ids);
    }
    return tx;
  }
  const byId = new Map(devices.map((d) => [d.objectId, d]));
  for (const { groupId, deviceId } of changes) {
    const known = inboxes ? inboxes.has(deviceId) : undefined;
    if (action === "add") appendAddDeviceToGroup(tx, groupId, byId.get(deviceId) ?? { objectId: deviceId }, known, fleet);
    else appendRemoveDeviceFromGroup(tx, groupId, deviceId, known, fleet);
  }
  return tx;
}

/**
 * How a transaction ended. `failed` is definite: refused before signing,
 * rejected by the wallet, or executed with failed effects, so (being atomic)
 * it applied nothing. `ambiguous`: it was, or may have been, submitted, but
 * no effects were confirmed (wallet transport error, confirmation timeout).
 */
export type BatchStatus = "succeeded" | "failed" | "ambiguous";
export interface BatchResult { index: number; status: BatchStatus; digest?: string; error?: string }

/**
 * The status of a transaction whose submission threw. Only a refusal before
 * the wallet was asked, or a definite failure reported by the wallet or
 * effects, proves nothing was applied.
 */
export function failureStatus(reachedWallet: boolean, definiteFailure: boolean): BatchStatus {
  return !reachedWallet || definiteFailure ? "failed" : "ambiguous";
}

/** Effects of a known digest: undefined while it is not found or cannot be read. */
export type DigestLookup = (digest: string) => Promise<"succeeded" | "failed" | undefined>;

/**
 * Resolve ambiguous results whose digest is known from that digest's
 * execution effects. Without a digest, or until the effects are found, they
 * stay ambiguous; a membership snapshot cannot show that a pending
 * transaction will not execute.
 */
export async function reconcileResults(results: BatchResult[], lookup: DigestLookup): Promise<BatchResult[]> {
  return Promise.all(results.map(async (result) => {
    if (result.status !== "ambiguous" || !result.digest) return result;
    const status = await lookup(result.digest).catch(() => undefined);
    return status ? { ...result, status } : result;
  }));
}

export interface MembershipOutcome {
  /** Applied: the transaction's effects were confirmed successful. */
  changed: MembershipChange[];
  /** In a transaction that definitely failed (atomic, so nothing applied). */
  unchanged: MembershipChange[];
  /** In a transaction that may still execute or whose effects were not confirmed. */
  unknown: MembershipChange[];
  /** In transactions after the failure; never submitted. */
  notAttempted: MembershipChange[];
}

/**
 * Classify every planned change after a run from transaction outcomes only.
 * Transactions are atomic: a successful one applied all its changes, a failed
 * one none. Later transactions were not sent.
 */
export function membershipOutcome(plan: MembershipPlan, results: BatchResult[]): MembershipOutcome {
  const outcome: MembershipOutcome = { changed: [], unchanged: [], unknown: [], notAttempted: [] };
  plan.transactions.forEach((changes, index) => {
    const result = results.find((r) => r.index === index);
    if (!result) outcome.notAttempted.push(...changes);
    else if (result.status === "succeeded") outcome.changed.push(...changes);
    else if (result.status === "failed") outcome.unchanged.push(...changes);
    else outcome.unknown.push(...changes);
  });
  return outcome;
}
