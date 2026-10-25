import { readShardedReceipts, readReceiptDirectory, buildReceiptMaintenanceTx } from "./receipt-shards";
import type { NamedFleet } from "./customer-fleets";
import { batchFields } from './batch-fields';
import { incrementalEvents as allEvents } from "./event-history";
import { readDeploymentControl, type DeploymentControl } from "./deployment-control";
import { calls } from "./fleet-protocol";
import { SUPPORTED_DEPLOYMENT_SCHEMA_VERSIONS } from "./protocol-version";
import { requireManifest, moveType, verifyDeployment } from "./deployment-manifest";
import { Transaction } from "@mysten/sui/transactions";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { normalizeStructTag } from "@mysten/sui/utils";
import { DeviceBcs, DeviceOwnerCapBcs, GroupBcs, GroupCreatedBcs, DeploymentCreatedBcs, DeploymentBcs, ReceiptsBcs } from "./move-bcs";
import { bcs } from "@mysten/sui/bcs";
import { PACKAGE_ID, REGISTRY_ID, DEVICE_TYPE, DEVICE_OWNER_CAP_TYPE, DEVICE_GROUP_TYPE, DEPLOYMENT_CREATED_EVENT } from "./constants";
import { announce, inboxId as inboxIdFor, inboxSupported, indexedMembers, withInbox, type Announcements } from "./fleet-inbox";

// ── Types ─────────────────────────────────────────────────────────────────────

/**
 * Operator-set Device metadata (SF-100). Bulk enrollment writes these after a
 * device has enrolled under the operator's claim; the on-chain `name` is the
 * device-chosen name and stays visible as `chainName`.
 */
export const ALIAS_META_KEY = "suifleet.alias";
export const SERIAL_META_KEY = "suifleet.serial";

export interface DeviceObject {
  objectId: string;
  /** The operator's alias when set, otherwise the registered name. */
  name: string;
  chainName: string;
  serial?: string;
  meta: Record<string, string>;
  deviceAddress: string;
  arch: string;
  registeredAt: number;
  managed?: boolean;
  /**
   * Set once the device is activated: the registry holds the Device and this
   * owner cap controls it. Unactivated devices (all those registered before
   * identities existed) cannot submit trusted reports.
   */
  ownerCapId?: string;
}

/** Only the fields transaction builders need. */
export type DeviceRef = Pick<DeviceObject, "objectId" | "ownerCapId">;

export interface DeviceGroup {
  objectId: string;
  name: string;
  description: string;
  admin: string;
  /** Every member: `device_ids` plus the MemberKey index (SF-125). */
  deviceIds: string[];
  /** Members still in `device_ids`; indexing moves them into the index. */
  unindexed: number;
}

export interface DeploymentEvent {
  control: DeploymentControl;
  deploymentId: string;
  schemaVersion: string;
  componentName: string;
  version: string;
  recipeBlobId: string;
  artifactSha256: string;
  targetGroups: string[];
  targetDevices: string[];
  creator: string;
  timestampMs: number;
  /** Signer- and target-checked statuses from the authoritative receipts. */
  statuses: Record<string, number>;
  /**
   * False for records created before receipts existed, or whose receipts were
   * last written by an older package: old package versions can forge legacy
   * statuses or accept reports from revoked keys, so none are shown.
   */
  receiptsVerified: boolean;
  receiptCleanup?: boolean;
  resolvedDeviceIds: string[];
}

export interface DeploymentReceipts {
  verified: boolean;
  statuses: Record<string, number>;
}

/**
 * Receipts authority version of the contracts this client targets. Lower
 * versions are written by older packages, which accept reports from revoked,
 * rotated-out or deregistered keys; the current package discards them on
 * migration. Version 3 requires the bound shared roots; unknown versions fail closed.
 */
export const RECEIPTS_AUTHORITY_VERSION = BigInt(2);

/**
 * Read authoritative status receipts. Legacy `StatusesKey` entries are never
 * used: any address can write them through the original package.
 */
export async function readReceipts(client: Pick<SuiGrpcClient, "getDynamicField" | "getObject" | "getObjects">, deploymentId: string): Promise<DeploymentReceipts> {
  let field;
  try {
    field = await client.getDynamicField({ parentId: deploymentId,
      name: { type: moveType("deployment_manager::ReceiptsKey"), bcs: bcs.bool().serialize(false).toBytes() } });
  } catch (error) {
    // Only a missing field is legacy-unverified; transport/permission errors
    // must remain visible instead of masquerading as an empty receipt list.
    if (error instanceof ObjectError && error.reason === "notFound") return { verified: false, statuses: {} };
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(moveType("deployment_manager::Receipts"))) throw new Error("Unexpected Receipts type");
  const receipts = ReceiptsBcs.parse(field.dynamicField.value.bcs);
  // This is an on-chain authority generation, not a client/layout version.
  // Additive upgrades can bump it while retaining the same BCS layout.
  if (BigInt(receipts.version) === BigInt(0)) throw new Error("Invalid Receipts authority version");
  if (receipts.statuses.contents.some((entry) => entry.value !== 1 && entry.value !== 2)) throw new Error("Unsupported Receipts status");
  if (receipts.version === "4") return readShardedReceipts(client, deploymentId);
  if (BigInt(receipts.version) > BigInt(4)) throw new Error("Unsupported receipts authority version");
  let managed = false;
  if (requireManifest().type_origins['deployment_manager::CustomerFleetKey']) {
    try {
      await client.getDynamicField({ parentId: deploymentId, name: { type: moveType('deployment_manager::CustomerFleetKey'), bcs: new Uint8Array([0]) } });
      managed = true;
    } catch (error) { if (!(error instanceof ObjectError && error.reason === 'notFound')) throw error; }
  }
  if (BigInt(receipts.version) !== (managed ? BigInt(3) : RECEIPTS_AUTHORITY_VERSION)) return { verified: false, statuses: {} };
  return { verified: true, statuses: Object.fromEntries(receipts.statuses.contents.map((entry) => [entry.key, entry.value])) };
}

// ── Device transactions ───────────────────────────────────────────────────────

/** Register, issue the device's cap and activate it; the owner cap goes to `sender`. */
export function buildRegisterDeviceTx(
  name: string,
  deviceAddress: string,
  arch: string,
  sender: string,
  fleet?: NamedFleet,
): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) {
    tx.moveCall({ target: `${PACKAGE_ID}::customer::register_device`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id),
      tx.object(fleet.budgetId), tx.object(REGISTRY_ID), tx.pure.string(name), tx.pure.address(deviceAddress), tx.pure.string(arch), tx.object("0x6")] });
    return tx;
  }
  const [device] = calls.device_registry_register_device(tx, PACKAGE_ID, REGISTRY_ID, name, deviceAddress, arch, "0x6");
  calls.device_cap_issue_cap(tx, PACKAGE_ID, device);
  const [owner] = calls.device_registry_activate_device(tx, PACKAGE_ID, REGISTRY_ID, device);
  tx.transferObjects([owner], tx.pure.address(sender));
  return tx;
}

/** Take an owned (unactivated) Device into registry custody so it is trusted. */
export function buildActivateDeviceTx(deviceObjectId: string, sender: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  const [owner] = calls.device_registry_activate_device(tx, PACKAGE_ID, REGISTRY_ID, deviceObjectId);
  tx.transferObjects([owner], tx.pure.address(sender));
  return tx;
}

/**
 * Pass the Device to a `&Device` call. An activated Device is borrowed from
 * custody and returned in the same transaction.
 */
function withDevice(tx: Transaction, device: DeviceRef, use: (device: Parameters<Transaction["object"]>[0]) => void) {
  if (!device.ownerCapId) return use(device.objectId);
  const [borrowed, loan] = calls.device_registry_borrow_device(tx, PACKAGE_ID, REGISTRY_ID, device.ownerCapId);
  use(borrowed);
  calls.device_registry_return_device(tx, PACKAGE_ID, REGISTRY_ID, borrowed, loan);
}

export function buildIssueCapTx(device: DeviceRef): Transaction {
  requireManifest();
  const tx = new Transaction();
  withDevice(tx, device, (target) => calls.device_cap_issue_cap(tx, PACKAGE_ID, target));
  return tx;
}

/** Deregistering an activated device leaves a revoked identity tombstone. */
export function buildDeregisterDeviceTx(device: DeviceRef): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (device.ownerCapId) calls.device_registry_deregister_activated_device(tx, PACKAGE_ID, REGISTRY_ID, device.ownerCapId);
  else calls.device_registry_deregister_device(tx, PACKAGE_ID, REGISTRY_ID, device.objectId);
  return tx;
}

// ── Group transactions ────────────────────────────────────────────────────────

export function buildCreateGroupTx(name: string, description: string, fleet?: NamedFleet): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::create_group`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.pure.string(name), tx.pure.string(description), tx.object("0x6")] });
  else calls.device_group_create_group(tx, PACKAGE_ID, name, description, "0x6");
  return tx;
}

/**
 * With inboxes (SF-125), `inboxExists` says whether the activated device's
 * inbox exists: the group is then listed in it (creating it if needed), so
 * the device finds the group without event history.
 */
export function buildAddDeviceToGroupTx(groupObjectId: string, device: DeviceRef, inboxExists?: boolean, fleet?: NamedFleet): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) {
    tx.moveCall({ target: `${PACKAGE_ID}::customer::add_to_group`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(groupObjectId), tx.pure.id(device.objectId)] });
    return tx;
  }
  const ownerCapId = device.ownerCapId;
  if (inboxSupported() && ownerCapId && inboxExists !== undefined) {
    // A new inbox is created before the custody loan: new_inbox requires
    // the device to be in the registry's custody.
    withInbox(tx, { deviceId: device.objectId, ownerCapId, exists: inboxExists }, (inbox) =>
      withDevice(tx, device, (target) => calls.fleet_inbox_add_member(tx, PACKAGE_ID, groupObjectId, inbox, target)));
  } else {
    withDevice(tx, device, (target) => calls.device_group_add_device(tx, PACKAGE_ID, groupObjectId, target));
  }
  return tx;
}

/** Also unlists the group from the device's inbox when it exists. */
export function buildRemoveDeviceFromGroupTx(groupObjectId: string, deviceId: string, inboxExists = false, fleet?: NamedFleet): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) {
    tx.moveCall({ target: `${PACKAGE_ID}::customer::remove_from_group`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(groupObjectId), tx.pure.id(deviceId)] });
    return tx;
  }
  if (inboxSupported() && inboxExists) {
    calls.fleet_inbox_remove_member(tx, PACKAGE_ID, groupObjectId, inboxIdFor(deviceId));
  } else {
    calls.device_group_remove_device(tx, PACKAGE_ID, groupObjectId, deviceId);
  }
  return tx;
}

export function buildDeleteGroupTx(groupObjectId: string, fleet?: NamedFleet): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::delete_group`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(groupObjectId)] });
  else calls.device_group_delete_group(tx, PACKAGE_ID, groupObjectId);
  return tx;
}

// ── Deployment transactions ───────────────────────────────────────────────────

export function buildCreateDeploymentTx(
  componentName: string,
  version: string,
  recipeBlobId: string,
  artifactSha256: string,
  recipeSha256: string,
  targetGroupIds: string[],
  targetDeviceIds: string[] = [],
  initiallyPaused = false,
  announcements?: Announcements,
  fleet?: NamedFleet,
): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (fleet) {
    const [record] = tx.moveCall({ target: `${PACKAGE_ID}::customer::new_deployment`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id),
      tx.pure.string(componentName), tx.pure.string(version), tx.pure.string(recipeBlobId), tx.pure.string(artifactSha256), tx.pure.string(recipeSha256),
      tx.pure.vector("id", targetGroupIds), tx.pure.vector("id", targetDeviceIds), tx.object("0x6")] });
    if (initiallyPaused) tx.moveCall({ target: `${PACKAGE_ID}::customer::set_control`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), record, tx.pure.u8(1), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0), tx.pure.u64(0)] });
    if (requireManifest().type_origins["deployment_manager::ReceiptShardKey"]) tx.moveCall({ target: `${PACKAGE_ID}::customer::begin_sharded_receipts`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), record] });
    calls.deployment_manager_share_deployment(tx, PACKAGE_ID, record);
    return tx;
  }
  if (initiallyPaused || announcements || requireManifest().type_origins["deployment_manager::ReceiptShardKey"]) {
    const [record] = calls.deployment_manager_new_deployment_verified(tx, PACKAGE_ID, componentName, version, recipeBlobId, artifactSha256, recipeSha256, targetGroupIds, targetDeviceIds, "0x6");
    if (initiallyPaused) calls.deployment_manager_set_control(tx, PACKAGE_ID, record, 1, 0, 0, 0, 0, 0);
    if (requireManifest().type_origins["deployment_manager::ReceiptShardKey"]) calls.deployment_manager_begin_sharded_receipts(tx, PACKAGE_ID, record);
    if (announcements) announce(tx, record, announcements);
    calls.deployment_manager_share_deployment(tx, PACKAGE_ID, record);
  } else {
    calls.deployment_manager_create_deployment_verified(tx, PACKAGE_ID, componentName, version, recipeBlobId, artifactSha256, recipeSha256, targetGroupIds, targetDeviceIds, "0x6");
  }
  return tx;
}

/** One bounded deletion step. Sharded deployments first stop admission and
 * reports, drain fields, retire roots, then consume the deployment. */
export async function buildDeleteDeploymentStepTx(client: Pick<SuiGrpcClient, "getDynamicField" | "getObject">, deployment: string, schema = "1", fleet?: NamedFleet): Promise<Transaction> {
  const directory = await readReceiptDirectory(client, deployment);
  return directory ? buildReceiptMaintenanceTx(client, deployment, "cleanup", fleet) : buildDeleteDeploymentTx(deployment, schema, fleet);
}

export function buildDeleteDeploymentTx(deploymentObjectId: string, schemaVersion = "1", fleet?: NamedFleet): Transaction {
  requireManifest();
  if (schemaVersion !== "1" && schemaVersion !== "2" && schemaVersion !== "3") throw new Error("Unsupported DeploymentRecord schema");
  const tx = new Transaction();
  if (fleet) { tx.moveCall({ target: `${PACKAGE_ID}::customer::delete_deployment`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(deploymentObjectId)] }); return tx; }
  if (schemaVersion === "2") calls.component_catalog_delete_deployment(tx, PACKAGE_ID, deploymentObjectId);
  else if (schemaVersion === "3") calls.deployment_manager_delete_rollout(tx, PACKAGE_ID, deploymentObjectId);
  else calls.deployment_manager_delete_deployment(tx, PACKAGE_ID, deploymentObjectId);
  return tx;
}

// ── Queries ───────────────────────────────────────────────────────────────────

export async function allOwned(client: SuiGrpcClient, owner: string, type: string) {
  let page = await client.listOwnedObjects({ owner, type, include: { content: true }, limit: 50 });
  const data = [...page.objects];
  while (page.hasNextPage) {
    if (!page.cursor) throw new Error("Device pagination returned no cursor");
    const cursor = page.cursor;
    page = await client.listOwnedObjects({ owner, type, include: { content: true }, limit: 50, cursor });
    if (page.hasNextPage && page.cursor === cursor) throw new Error("Device pagination made no progress");
    data.push(...page.objects);
  }
  return data;
}

/** Activated devices (through their owner caps) and owned, unactivated ones. */
export async function fetchDevices(client: SuiGrpcClient, owner: string): Promise<DeviceObject[]> {
  await verifyDeployment(client);
  const caps = (await allOwned(client, owner, DEVICE_OWNER_CAP_TYPE)).map((object) => ({
    ownerCapId: object.objectId, deviceId: DeviceOwnerCapBcs.parse(object.content).device_id }));
  const custodied = await objectsInBatches(client, caps.map((cap) => cap.deviceId));
  const ownerCaps = new Map(caps.map((cap) => [cap.deviceId, cap.ownerCapId]));
  const devices = [...custodied, ...await allOwned(client, owner, DEVICE_TYPE)];
  return devices.map((object) => {
    const fields = DeviceBcs.parse(object.content);
    if (fields.schema_version !== "1") throw new Error("Unsupported Device schema");
    const meta = Object.fromEntries(fields.meta.contents.map((entry) => [entry.key, entry.value]));
    return { objectId: object.objectId, name: meta[ALIAS_META_KEY] || fields.name, chainName: fields.name, serial: meta[SERIAL_META_KEY] || undefined,
      meta, deviceAddress: fields.device_address, arch: fields.arch, registeredAt: Number(fields.registered_at), ownerCapId: ownerCaps.get(object.objectId) };
  });
}

/** Exact managed inventory; authorization comes from verified Customer/Fleet state. */
export async function fetchFleetDevices(client: SuiGrpcClient, ids: string[]): Promise<DeviceObject[]> {
  const devices = await objectsInBatches(client, ids);
  if (devices.length !== ids.length) throw Error("Fleet device inventory is incomplete");
  return devices.map(object => {
    if (normalizeStructTag(object.type) !== normalizeStructTag(DEVICE_TYPE)) throw Error("Unexpected fleet device type");
    const fields = DeviceBcs.parse(object.content);
    const meta = Object.fromEntries(fields.meta.contents.map(entry => [entry.key, entry.value]));
    return { objectId: object.objectId, name: meta[ALIAS_META_KEY] || fields.name, chainName: fields.name,
      serial: meta[SERIAL_META_KEY] || undefined, meta, deviceAddress: fields.device_address,
      arch: fields.arch, registeredAt: Number(fields.registered_at), managed: true };
  });
}

export async function fetchGroups(client: SuiGrpcClient, owner: string): Promise<DeviceGroup[]> {
  await verifyDeployment(client);
  // Creation sender is not the current admin after recipient-confirmed handover.
  const events = await allEvents(client, moveType("device_group::GroupCreated"));
  const ids = events.map((event) => GroupCreatedBcs.parse(event.bcs))
    .map((event) => event.group_id);
  const objects = await objectsInBatches(client, [...new Set(ids)]);
  const groups = objects.map((object) => {
    const fields = GroupBcs.parse(object.content);
    if (fields.schema_version !== "1") throw new Error("Unsupported DeviceGroup schema");
    return { objectId: object.objectId, name: fields.name, description: fields.description,
      admin: fields.admin, deviceIds: fields.device_ids, unindexed: fields.device_ids.length };
  }).filter((group) => group.admin === owner);
  return Promise.all(groups.map(async (group) => inboxSupported()
    ? { ...group, deviceIds: [...new Set([...group.deviceIds, ...await indexedMembers(client, group.objectId)])] }
    : group));
}

export { incrementalEvents as allEvents } from "./event-history";

export async function objectsInBatches(client: SuiGrpcClient, ids: string[]) {
  const objects = [];
  for (let index = 0; index < ids.length; index += 50) {
    const batch = await client.getObjects({ objectIds: ids.slice(index, index + 50), include: { content: true } });
    for (const object of batch.objects) {
      if (object instanceof ObjectError && (object.reason === "notFound" || object.reason === "deleted")) continue;
      if (object instanceof Error) throw object;
      objects.push(object);
    }
  }
  return objects;
}

// Deployment targets are arbitrary IDs on chain. An invalid group must not
// hide unrelated deployments; transport errors still escape objectsInBatches.
function supportedTargetGroup(group: { type: string; content: Uint8Array }) {
  try {
    if (normalizeStructTag(group.type) !== normalizeStructTag(DEVICE_GROUP_TYPE)) return undefined;
    const fields = GroupBcs.parse(group.content);
    return fields.schema_version === "1" ? fields : undefined;
  } catch {
    return undefined; // Invalid type tag or malformed BCS for this one target.
  }
}

export async function fetchDeploymentEvents(client: SuiGrpcClient, owner?: string): Promise<DeploymentEvent[]> {
  await verifyDeployment(client);
  // Delegated creation and handover can make creator differ from event sender.
  const events = (await allEvents(client, DEPLOYMENT_CREATED_EVENT)).map((event) => DeploymentCreatedBcs.parse(event.bcs));
  const objects = await objectsInBatches(client, [...new Set(events.map((event) => event.deployment_id))]);
  const live = new Map(objects.map((object) => [object.objectId, DeploymentBcs.parse(object.content)]));
  const liveEvents = events.filter((event) => {
    const record = live.get(event.deployment_id);
    return record && (!owner || record.creator === owner);
  });
  const directories = new Map(await Promise.all(liveEvents.map(async event =>
    [event.deployment_id, await readReceiptDirectory(client, event.deployment_id)] as const)));
  // Persisted events discover IDs only. Targets must be re-read from objects:
  // browser storage is neither a current target list nor an authority source.
  const targetValues = await batchFields(client, liveEvents.flatMap(event =>
    (["TargetGroupsKey", "TargetDevicesKey"] as const).map(name => ({ parent: event.deployment_id,
      keyType: name === "TargetGroupsKey" && !!directories.get(event.deployment_id) ? moveType("deployment_manager::ShardedTargetGroupsKey") : moveType(`deployment_manager::${name}`), key: bcs.bool().serialize(false).toBytes(),
      valueType: "vector<0x2::object::ID>" }))));
  const currentEvents = liveEvents.map((event, index) => {
    const groups = targetValues[index * 2], devices = targetValues[index * 2 + 1];
    if (!groups) throw Error("Missing deployment target groups");
    return { ...event, target_groups: bcs.vector(bcs.Address).parse(groups),
      target_devices: devices ? bcs.vector(bcs.Address).parse(devices) : [] };
  });
  const groups = await objectsInBatches(client, [...new Set(currentEvents.flatMap((event) => event.target_groups))]);
  const groupsById = new Map(groups.map((group) => [group.objectId, supportedTargetGroup(group)]));
  // Indexed members (SF-125) are dynamic fields, not `device_ids`.
  const indexed = new Map(inboxSupported() ? await Promise.all([...groupsById].filter(([, group]) => group)
    .map(async ([id]) => [id, await indexedMembers(client, id)] as const)) : []);
  return Promise.all(currentEvents.map(async (event) => {
    const record = live.get(event.deployment_id)!;
    if (!SUPPORTED_DEPLOYMENT_SCHEMA_VERSIONS.some(v => BigInt(v) === BigInt(record.schema_version))) throw new Error("Unsupported DeploymentRecord schema");
    const resolvedDeviceIds = [...new Set([...event.target_devices, ...event.target_groups.flatMap((id) => {
      const group = groupsById.get(id);
      return group && group.admin === record.creator ? [...group.device_ids, ...indexed.get(id) ?? []] : [];
    })])];
    const { verified, statuses } = await readReceipts(client, event.deployment_id);
    return { control: await readDeploymentControl(client, event.deployment_id), deploymentId: event.deployment_id, receiptCleanup: directories.get(event.deployment_id)?.retiring, schemaVersion: directories.get(event.deployment_id)?.original_schema ?? record.schema_version, componentName: record.component_name, version: record.version,
      recipeBlobId: record.recipe_blob_id, artifactSha256: record.artifact_sha256,
      targetGroups: event.target_groups, targetDevices: event.target_devices,
      creator: record.creator, timestampMs: Number(record.created_at), statuses, receiptsVerified: verified,
      resolvedDeviceIds: [...new Set([...resolvedDeviceIds, ...Object.keys(statuses)])] };
  }));
}
