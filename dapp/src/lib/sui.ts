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
}

// ── Device transactions ───────────────────────────────────────────────────────

export function buildRegisterDeviceTx(
  name: string,
  deviceAddress: string,
  arch: string,
  sender: string,
): Transaction {
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
  tx.transferObjects([device], tx.pure.address(sender));
  return tx;
}

export function buildIssueCapTx(deviceObjectId: string): Transaction {
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::device_cap::issue_cap`,
    arguments: [tx.object(deviceObjectId)],
  });
  return tx;
}

export function buildDeregisterDeviceTx(deviceObjectId: string): Transaction {
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::device_registry::deregister_device`,
    arguments: [tx.object(REGISTRY_ID), tx.object(deviceObjectId)],
  });
  return tx;
}

// ── Group transactions ────────────────────────────────────────────────────────

export function buildCreateGroupTx(name: string, description: string): Transaction {
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
  targetGroupIds: string[],
  targetDeviceIds: string[] = [],
): Transaction {
  const tx = new Transaction();
  tx.moveCall({
    target: `${PACKAGE_ID}::deployment_manager::create_deployment`,
    arguments: [
      tx.pure.string(componentName),
      tx.pure.string(version),
      tx.pure.string(recipeBlobId),
      tx.pure.string(artifactSha256),
      tx.pure(bcs.vector(bcs.Address).serialize(targetGroupIds)),
      tx.pure(bcs.vector(bcs.Address).serialize(targetDeviceIds)),
      tx.object("0x6"),
    ],
  });
  return tx;
}

export function buildDeleteDeploymentTx(deploymentObjectId: string): Transaction {
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
  const res = await client.getOwnedObjects({
    owner,
    filter: { StructType: DEVICE_TYPE },
    options: { showContent: true },
  });

  return res.data.flatMap((obj) => {
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
  // Groups are shared objects, but we created them so we look by event.
  // Alternatively, query shared objects by type filtered to admin=owner.
  // For now, query all DeviceGroup objects the owner created via events, or
  // use getOwnedObjects with the type (works because we use dynamic field trick).
  // Since DeviceGroup is shared (not owned), we query events for GroupCreated.
  const res = await client.queryEvents({
    query: { MoveEventType: `${DEVICE_GROUP_TYPE.replace("::DeviceGroup", "")}::GroupCreated` },
    limit: 50,
    order: "descending",
  });

  const groupIds = res.data
    .filter((ev) => (ev.parsedJson as Record<string, unknown>).admin === owner)
    .map((ev) => (ev.parsedJson as Record<string, unknown>).group_id as string);

  if (groupIds.length === 0) return [];

  const objects = await client.multiGetObjects({
    ids: groupIds,
    options: { showContent: true },
  });

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

export async function fetchDeploymentEvents(
  client: SuiClient,
): Promise<DeploymentEvent[]> {
  const res = await client.queryEvents({
    query: { MoveEventType: DEPLOYMENT_CREATED_EVENT },
    order: "descending",
    limit: 50,
  });

  return res.data.map((ev) => {
    const p = ev.parsedJson as Record<string, unknown>;
    return {
      deploymentId: p.deployment_id as string,
      componentName: p.component_name as string,
      version: p.version as string,
      recipeBlobId: p.recipe_blob_id as string,
      artifactSha256: p.artifact_sha256 as string,
      targetGroups: (p.target_groups as string[]) ?? [],
      targetDevices: (p.target_devices as string[]) ?? [],
      creator: p.creator as string,
      timestampMs: Number(ev.timestampMs ?? 0),
    };
  });
}
