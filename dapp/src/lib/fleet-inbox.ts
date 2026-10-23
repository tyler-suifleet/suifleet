/**
 * Device inboxes, group heads and the per-member group index (SF-125).
 *
 * Deployment transactions announce the new record to each target group the
 * sender administers and each target device it operates, so devices read
 * their state from objects instead of event history. Announcements are hints:
 * devices still verify the record, its creator and group membership.
 * Deployments created without an announcement (catalog deployments, older
 * clients, delegated roles) still reach devices through their bounded event
 * scan. Absent inbox type origins, every builder keeps its previous calls.
 */
import { bcs } from "@mysten/sui/bcs";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { Transaction } from "@mysten/sui/transactions";
import { ObjectError } from "@mysten/sui/client";
import { deriveObjectID, normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import { PACKAGE_ID, REGISTRY_ID } from "./constants";
import { MANIFEST, moveType, type DeploymentManifest } from "./deployment-manifest";
import { calls } from "./fleet-protocol";
import { INBOX_TYPES } from "./protocol-version";

type ObjectArg = Parameters<Transaction["object"]>[0];

/** Members moved into the index per transaction. */
export const INDEX_BATCH = 200;

export function inboxSupported(m: DeploymentManifest | undefined = MANIFEST): boolean {
  return !!m && INBOX_TYPES.every((name) => name in m.type_origins);
}

/** The device's inbox address, derived like `fleet_inbox::inbox_address`. */
export function inboxId(deviceId: string): string {
  return deriveObjectID(REGISTRY_ID, moveType("fleet_inbox::InboxKey"), bcs.Address.serialize(deviceId).toBytes());
}

/** Devices (of `deviceIds`) whose inbox already exists. */
export async function existingInboxes(client: Pick<SuiGrpcClient, "getObjects">, deviceIds: string[]): Promise<Set<string>> {
  const found = new Set<string>();
  const expected = normalizeStructTag(moveType("fleet_inbox::DeviceInbox"));
  for (let index = 0; index < deviceIds.length; index += 50) {
    const batch = deviceIds.slice(index, index + 50);
    const { objects } = await client.getObjects({ objectIds: batch.map(inboxId) });
    objects.forEach((object, i) => {
      if (object instanceof ObjectError && (object.reason === "notFound" || object.reason === "deleted")) return;
      if (object instanceof Error) throw object;
      if (normalizeStructTag(object.type) !== expected) throw new Error(`Unexpected object at inbox ${object.objectId}`);
      found.add(batch[i]);
    });
  }
  return found;
}

/** A device the sender operates: its owner cap proves it, the inbox may be new. */
export interface InboxTarget { deviceId: string; ownerCapId: string; exists: boolean }

/** Use the device's inbox, creating and sharing it in this transaction if needed. */
export function withInbox(tx: Transaction, target: InboxTarget, use: (inbox: ObjectArg) => void) {
  if (target.exists) return use(inboxId(target.deviceId));
  const [inbox] = calls.fleet_inbox_new_inbox(tx, PACKAGE_ID, REGISTRY_ID, target.ownerCapId);
  use(inbox);
  calls.fleet_inbox_share_inbox(tx, PACKAGE_ID, inbox);
}

export interface Announcements { groups: string[]; devices: InboxTarget[] }

/** Announce an unshared record to its groups and devices (before share_deployment). */
export function announce(tx: Transaction, record: ObjectArg, targets: Announcements) {
  for (const group of targets.groups) calls.fleet_inbox_announce_group(tx, PACKAGE_ID, group, record, "0x6");
  for (const device of targets.devices) {
    withInbox(tx, device, (inbox) =>
      calls.fleet_inbox_announce_device(tx, PACKAGE_ID, inbox, REGISTRY_ID, device.ownerCapId, record, "0x6"));
  }
}

/**
 * What `sender` may announce for these targets: groups it administers and
 * devices it holds owner caps for. Others (which the contract would refuse)
 * are left to the devices' event scan. Undefined without inbox support.
 */
export function announcementsFor(
  groups: { objectId: string; admin: string }[], devices: { objectId: string; ownerCapId?: string }[],
  sender: string, targetGroups: string[], targetDevices: string[], inboxes: Set<string>,
): Announcements | undefined {
  if (!inboxSupported()) return undefined;
  const me = normalizeSuiAddress(sender);
  const administered = new Set(groups.filter((g) => normalizeSuiAddress(g.admin) === me).map((g) => normalizeSuiAddress(g.objectId)));
  const caps = new Map(devices.filter((d) => d.ownerCapId).map((d) => [normalizeSuiAddress(d.objectId), d.ownerCapId!]));
  return {
    groups: targetGroups.filter((id) => administered.has(normalizeSuiAddress(id))),
    devices: targetDevices.flatMap((id) => {
      const ownerCapId = caps.get(normalizeSuiAddress(id));
      return ownerCapId ? [{ deviceId: id, ownerCapId, exists: inboxes.has(id) }] : [];
    }),
  };
}

/** Members held in the group's MemberKey index (not its `device_ids`). */
export async function indexedMembers(client: Pick<SuiGrpcClient, "listDynamicFields">, groupId: string): Promise<string[]> {
  const memberKey = normalizeStructTag(moveType("device_group::MemberKey"));
  const members: string[] = [];
  let cursor: string | null | undefined;
  for (;;) {
    const page = await client.listDynamicFields({ parentId: groupId, limit: 50, ...(cursor ? { cursor } : {}) });
    for (const field of page.dynamicFields) {
      if (normalizeStructTag(field.name.type) === memberKey) members.push(bcs.Address.parse(field.name.bcs));
    }
    if (!page.hasNextPage) return members;
    if (!page.cursor || page.cursor === cursor) throw new Error("Group member pagination made no progress");
    cursor = page.cursor;
  }
}

/**
 * Move up to INDEX_BATCH members of an unindexed (or partly indexed) group
 * into its index, and list the group in the inboxes of devices the sender
 * operates. Run again until `device_ids` is empty. Older clients see only
 * `device_ids`: index a group once its devices and tools run SF-125.
 */
export function buildIndexGroupTx(groupId: string, members: InboxTarget[]): Transaction {
  if (!inboxSupported()) throw new Error("This deployment has no group index");
  const tx = new Transaction();
  calls.device_group_index_members(tx, PACKAGE_ID, groupId, INDEX_BATCH);
  for (const member of members) withInbox(tx, member, (inbox) => calls.fleet_inbox_follow_group(tx, PACKAGE_ID, inbox, groupId));
  return tx;
}
