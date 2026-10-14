import { requireManifest, moveType, verifyDeployment } from "./deployment-manifest";
import { Transaction } from "@mysten/sui/transactions";
import { SuiClient } from "@mysten/sui/client";
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
  statuses: Record<string, number>;
  resolvedDeviceIds: string[];
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
  const [device] = tx.moveCall({
    target: `${PACKAGE_ID}::device_registry::register_device`,
    arguments: [
      tx.object(REGISTRY_ID),
      tx.pure.string(name),
      tx.pure.address(deviceAddress),
      tx.pure.string(arch),
      tx.object("0x6"),
    ],
  });
  tx.moveCall({ target: `${PACKAGE_ID}::device_cap::issue_cap`, arguments: [device] });
  tx.transferObjects([device], tx.pure.address(sender));
  return tx;
}

export function buildIssueCapTx(deviceObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::device_cap::issue_cap`,
    arguments: [tx.object(deviceObjectId)],
  });
  return tx;
}

export function buildDeregisterDeviceTx(deviceObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::device_registry::deregister_device`,
    arguments: [tx.object(REGISTRY_ID), tx.object(deviceObjectId)],
  });
  return tx;
}

// ── Group transactions ────────────────────────────────────────────────────────

export function buildCreateGroupTx(name: string, description: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::device_group::create_group`,
    arguments: [
      tx.pure.string(name),
      tx.pure.string(description),
      tx.object("0x6"),
    ],
  });
  return tx;
}

export function buildAddDeviceToGroupTx(groupObjectId: string, deviceObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::device_group::add_device`,
    arguments: [
      tx.object(groupObjectId),
      tx.object(deviceObjectId),
    ],
  });
  return tx;
}

export function buildRemoveDeviceFromGroupTx(groupObjectId: string, deviceId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::device_group::remove_device`,
    arguments: [
      tx.object(groupObjectId),
      tx.pure(bcs.Address.serialize(deviceId)),
    ],
  });
  return tx;
}

export function buildDeleteGroupTx(groupObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::device_group::delete_group`,
    arguments: [tx.object(groupObjectId)],
  });
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
  tx.moveCall({
    target: `${PACKAGE_ID}::deployment_manager::create_deployment_verified`,
    arguments: [
      tx.pure.string(componentName),
      tx.pure.string(version),
      tx.pure.string(recipeBlobId),
      tx.pure.string(artifactSha256),
      tx.pure.string(recipeSha256),
      tx.pure(bcs.vector(bcs.Address).serialize(targetGroupIds)),
      tx.pure(bcs.vector(bcs.Address).serialize(targetDeviceIds)),
      tx.object("0x6"),
    ],
  });
  return tx;
}

export function buildDeleteDeploymentTx(deploymentObjectId: string): Transaction {
  requireManifest();
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::deployment_manager::delete_deployment`,
    arguments: [tx.object(deploymentObjectId)],
  });
  return tx;
}

// ── Queries ───────────────────────────────────────────────────────────────────

export async function fetchDevices(
  client: SuiClient,
  owner: string,
): Promise<DeviceObject[]> {
  await verifyDeployment(client);
  const data = [];
  let cursor: string | null | undefined = null;
  do {
    const page = await client.getOwnedObjects({ owner, filter: { StructType: DEVICE_TYPE },
      options: { showContent: true }, cursor, limit: 50 });
    data.push(...page.data);
    cursor = page.hasNextPage ? page.nextCursor : null;
  } while (cursor);

  return data.flatMap((obj) => {
    const content = obj.data?.content;
    if (content?.dataType !== "moveObject") return [];
    const fields = content.fields as Record<string, unknown>;
    return [{
      objectId: obj.data!.objectId,
      name: fields.name as string,
      deviceAddress: fields.device_address as string,
      arch: fields.arch as string,
      registeredAt: Number(fields.registered_at),
    }];
  });
}

export async function fetchGroups(
  client: SuiClient,
  owner: string,
): Promise<DeviceGroup[]> {
  await verifyDeployment(client);
  const data = await allEvents(client, moveType("device_group::GroupCreated"));

  const groupIds = data
    .filter((ev) => (ev.parsedJson as Record<string, unknown>).admin === owner)
    .map((ev) => (ev.parsedJson as Record<string, unknown>).group_id as string);

  if (groupIds.length === 0) return [];

  const objects = await objectsInBatches(client, [...new Set(groupIds)]);

  return objects.flatMap((obj) => {
    const content = obj.data?.content;
    if (content?.dataType !== "moveObject") return [];
    const fields = content.fields as Record<string, unknown>;
    return [{
      objectId: obj.data!.objectId,
      name: fields.name as string,
      description: fields.description as string,
      admin: fields.admin as string,
      deviceIds: (fields.device_ids as string[]) ?? [],
    }];
  });
}

async function allEvents(client: SuiClient, type: string) {
  const data = [];
  let cursor: Parameters<SuiClient["queryEvents"]>[0]["cursor"] = null;
  do {
    const page = await client.queryEvents({ query: { MoveEventType: type }, order: "descending", limit: 50, cursor });
    data.push(...page.data);
    cursor = page.hasNextPage ? page.nextCursor : null;
  } while (cursor);
  return data;
}

async function objectsInBatches(client: SuiClient, ids: string[]) {
  const objects = [];
  for (let index = 0; index < ids.length; index += 50) {
    objects.push(...await client.multiGetObjects({ ids: ids.slice(index, index + 50), options: { showContent: true } }));
  }
  return objects;
}

export async function fetchDeploymentEvents(client: SuiClient, owner?: string): Promise<DeploymentEvent[]> {
  await verifyDeployment(client);
  const events = (await allEvents(client, DEPLOYMENT_CREATED_EVENT))
    .filter((event) => !owner || (event.parsedJson as Record<string, unknown>).creator === owner);
  const objects = await objectsInBatches(client, events.map((event) => (event.parsedJson as Record<string, unknown>).deployment_id as string));
  const live = new Set(objects.filter((object) => object.data?.content?.dataType === "moveObject").map((object) => object.data!.objectId));
  return Promise.all(events.filter((event) => live.has((event.parsedJson as Record<string, unknown>).deployment_id as string)).map(async (event) => {
    const p = event.parsedJson as Record<string, unknown>;
    const id = p.deployment_id as string;
    const groups = await objectsInBatches(client, (p.target_groups as string[]) ?? []);
    const resolvedDeviceIds = [...new Set([
      ...((p.target_devices as string[]) ?? []),
      ...groups.flatMap((group) => group.data?.content?.dataType === "moveObject"
        ? ((group.data.content.fields as Record<string, unknown>).device_ids as string[] ?? []) : []),
    ])];
    const statusObject = await client.getDynamicFieldObject({ parentId: id,
      name: { type: moveType("deployment_manager::StatusesKey"), value: {} } });
    if (statusObject.error) throw new Error(`Unable to load deployment status: ${statusObject.error.code}`);
    const content = statusObject.data?.content;
    const fields = content?.dataType === "moveObject" ? content.fields as Record<string, unknown> : {};
    const value = fields.value as { fields?: { contents?: { fields: { key: string; value: number } }[] } } | undefined;
    const statuses = Object.fromEntries((value?.fields?.contents ?? []).map((entry) => [entry.fields.key, Number(entry.fields.value)]));
    return { deploymentId: id, componentName: p.component_name as string, version: p.version as string,
      recipeBlobId: p.recipe_blob_id as string, artifactSha256: p.artifact_sha256 as string,
      targetGroups: (p.target_groups as string[]) ?? [], targetDevices: (p.target_devices as string[]) ?? [],
      creator: p.creator as string, timestampMs: Number(event.timestampMs ?? 0), statuses, resolvedDeviceIds: [...new Set([...resolvedDeviceIds, ...Object.keys(statuses)])] };
  }));
}
