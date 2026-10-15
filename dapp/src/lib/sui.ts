import { calls } from "./fleet-protocol";
import { SUPPORTED_DEPLOYMENT_SCHEMA_VERSIONS } from "./protocol-version";
import { requireManifest, moveType, verifyDeployment } from "./deployment-manifest";
import { Transaction } from "@mysten/sui/transactions";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { normalizeStructTag } from "@mysten/sui/utils";
import { DeviceBcs, GroupBcs, GroupCreatedBcs, DeploymentCreatedBcs, DeploymentBcs, ReceiptsBcs } from "./move-bcs";
import { bcs } from "@mysten/sui/bcs";
import { PACKAGE_ID, REGISTRY_ID, DEVICE_TYPE, DEVICE_GROUP_TYPE, DEPLOYMENT_CREATED_EVENT } from "./constants";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface DeviceObject {
  objectId: string;
  name: string;
  deviceAddress: string;
  arch: string;
  registeredAt: number;
}

export interface DeviceGroup {
  objectId: string;
  name: string;
  description: string;
  admin: string;
  deviceIds: string[];
}

export interface DeploymentEvent {
  deploymentId: string;
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
   * False for records created before receipts existed: their only statuses are
   * legacy entries that old package versions can forge, so none are shown.
   */
  receiptsVerified: boolean;
  resolvedDeviceIds: string[];
}

export interface DeploymentReceipts {
  verified: boolean;
  statuses: Record<string, number>;
}

/**
 * Read authoritative status receipts. Legacy `StatusesKey` entries are never
 * used: any address can write them through the original package.
 */
export async function readReceipts(client: Pick<SuiGrpcClient, "getDynamicField">, deploymentId: string): Promise<DeploymentReceipts> {
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
  return { verified: true, statuses: Object.fromEntries(receipts.statuses.contents.map((entry) => [entry.key, entry.value])) };
}

// ── Device transactions ───────────────────────────────────────────────────────

export function buildRegisterDeviceTx(
  name: string,
  deviceAddress: string,
  arch: string,
  sender: string,
): Transaction {
  requireManifest();
  const tx = new Transaction();
  const [device] = calls.device_registry_register_device(tx, PACKAGE_ID, REGISTRY_ID, name, deviceAddress, arch, "0x6");
  calls.device_cap_issue_cap(tx, PACKAGE_ID, device);
  tx.transferObjects([device], tx.pure.address(sender));
  return tx;
}

export function buildIssueCapTx(deviceObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  calls.device_cap_issue_cap(tx, PACKAGE_ID, deviceObjectId);
  return tx;
}

export function buildDeregisterDeviceTx(deviceObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  calls.device_registry_deregister_device(tx, PACKAGE_ID, REGISTRY_ID, deviceObjectId);
  return tx;
}

// ── Group transactions ────────────────────────────────────────────────────────

export function buildCreateGroupTx(name: string, description: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  calls.device_group_create_group(tx, PACKAGE_ID, name, description, "0x6");
  return tx;
}

export function buildAddDeviceToGroupTx(groupObjectId: string, deviceObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  calls.device_group_add_device(tx, PACKAGE_ID, groupObjectId, deviceObjectId);
  return tx;
}

export function buildRemoveDeviceFromGroupTx(groupObjectId: string, deviceId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  calls.device_group_remove_device(tx, PACKAGE_ID, groupObjectId, deviceId);
  return tx;
}

export function buildDeleteGroupTx(groupObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  calls.device_group_delete_group(tx, PACKAGE_ID, groupObjectId);
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
): Transaction {
  requireManifest();
  const tx = new Transaction();
  calls.deployment_manager_create_deployment_verified(tx, PACKAGE_ID, componentName, version, recipeBlobId, artifactSha256, recipeSha256, targetGroupIds, targetDeviceIds, "0x6");
  return tx;
}

export function buildDeleteDeploymentTx(deploymentObjectId: string, catalog = false): Transaction {
  requireManifest();
  const tx = new Transaction();
  if (catalog) calls.component_catalog_delete_deployment(tx, PACKAGE_ID, deploymentObjectId);
  else calls.deployment_manager_delete_deployment(tx, PACKAGE_ID, deploymentObjectId);
  return tx;
}

// ── Queries ───────────────────────────────────────────────────────────────────

export async function fetchDevices(client: SuiGrpcClient, owner: string): Promise<DeviceObject[]> {
  await verifyDeployment(client);
  let page = await client.listOwnedObjects({ owner, type: DEVICE_TYPE, include: { content: true }, limit: 50 });
  const data = [...page.objects];
  while (page.hasNextPage) {
    if (!page.cursor) throw new Error("Device pagination returned no cursor");
    const cursor = page.cursor;
    page = await client.listOwnedObjects({ owner, type: DEVICE_TYPE, include: { content: true }, limit: 50, cursor });
    if (page.hasNextPage && page.cursor === cursor) throw new Error("Device pagination made no progress");
    data.push(...page.objects);
  }
  return data.map((object) => {
    const fields = DeviceBcs.parse(object.content);
    if (fields.schema_version !== "1") throw new Error("Unsupported Device schema");
    return { objectId: object.objectId, name: fields.name, deviceAddress: fields.device_address,
      arch: fields.arch, registeredAt: Number(fields.registered_at) };
  });
}

export async function fetchGroups(client: SuiGrpcClient, owner: string): Promise<DeviceGroup[]> {
  await verifyDeployment(client);
  const events = await allEvents(client, moveType("device_group::GroupCreated"));
  const ids = events.map((event) => GroupCreatedBcs.parse(event.bcs))
    .filter((event) => event.admin === owner).map((event) => event.group_id);
  const objects = await objectsInBatches(client, [...new Set(ids)]);
  return objects.map((object) => {
    const fields = GroupBcs.parse(object.content);
    if (fields.schema_version !== "1") throw new Error("Unsupported DeviceGroup schema");
    return { objectId: object.objectId, name: fields.name, description: fields.description,
      admin: fields.admin, deviceIds: fields.device_ids };
  });
}

export async function allEvents(client: SuiGrpcClient, type: string) {
  let page = await client.listEvents({ filter: { eventType: type }, order: "descending", limit: 50 });
  const events = [...page.events];
  // A scan-limited gRPC page may be empty and still have more history.
  while (page.hasNextPage) {
    if (!page.endCursor) throw new Error("Event pagination returned no cursor");
    const before = page.endCursor;
    page = await client.listEvents({ filter: { eventType: type }, before, limit: 50 });
    if (page.hasNextPage && page.endCursor === before) throw new Error("Event pagination made no progress");
    events.push(...page.events);
  }
  return events;
}

async function objectsInBatches(client: SuiGrpcClient, ids: string[]) {
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
  const events = (await allEvents(client, DEPLOYMENT_CREATED_EVENT)).map((event) => DeploymentCreatedBcs.parse(event.bcs))
    .filter((event) => !owner || event.creator === owner);
  const objects = await objectsInBatches(client, [...new Set(events.map((event) => event.deployment_id))]);
  const live = new Map(objects.map((object) => [object.objectId, DeploymentBcs.parse(object.content)]));
  const liveEvents = events.filter((event) => live.has(event.deployment_id));
  const groups = await objectsInBatches(client, [...new Set(liveEvents.flatMap((event) => event.target_groups))]);
  const groupsById = new Map(groups.map((group) => [group.objectId, supportedTargetGroup(group)]));
  return Promise.all(liveEvents.map(async (event) => {
    const record = live.get(event.deployment_id)!;
    if (!SUPPORTED_DEPLOYMENT_SCHEMA_VERSIONS.some(v => BigInt(v) === BigInt(record.schema_version))) throw new Error("Unsupported DeploymentRecord schema");
    const resolvedDeviceIds = [...new Set([...event.target_devices, ...event.target_groups.flatMap((id) => {
      const group = groupsById.get(id);
      return group && group.admin === record.creator ? group.device_ids : [];
    })])];
    const { verified, statuses } = await readReceipts(client, event.deployment_id);
    return { deploymentId: event.deployment_id, componentName: record.component_name, version: record.version,
      recipeBlobId: record.recipe_blob_id, artifactSha256: record.artifact_sha256,
      targetGroups: event.target_groups, targetDevices: event.target_devices,
      creator: record.creator, timestampMs: Number(record.created_at), statuses, receiptsVerified: verified,
      resolvedDeviceIds: [...new Set([...resolvedDeviceIds, ...Object.keys(statuses)])] };
  }));
}
