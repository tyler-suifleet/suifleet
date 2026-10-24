// Operations use the same verified chain readers and local builders as the dApp.
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction } from '@mysten/sui/transactions';
import { normalizeSuiAddress as id } from '@mysten/sui/utils';
import { calls } from './fleet-protocol';
import { PACKAGE_ID, REGISTRY_ID } from './constants';
import { fetchDevices, fetchGroups, buildCreateDeploymentTx, type DeviceObject, type DeviceGroup } from './sui';
import { readIdentities } from './telemetry';

export interface Operation {
  command: 'add-devices' | 'remove-devices' | 'create-deployment';
  sender: string;
  devices?: string[];
  group?: string;
  component?: string;
  version?: string;
  recipeBlob?: string;
  artifactSha256?: string;
  recipeSha256?: string;
  targetGroups?: string[];
  targetDevices?: string[];
}
const unique = (ids: string[] = []) => {
  if (ids.length > 200 || new Set(ids.map(value => id(value))).size !== ids.length) throw Error('At most 200 distinct IDs per list');
  return ids.map(value => id(value));
};
/** Pure intent planning, separated from chain reads for independent call checks. */
export function planOperation(request: Operation, devices: DeviceObject[], groups: DeviceGroup[], active: Set<string>): Transaction {
  if (!['add-devices', 'remove-devices', 'create-deployment'].includes(request.command)) throw Error('Unknown operation');
  const sender = id(request.sender);
  const owned = new Map(devices.map(d => [id(d.objectId), d]));
  const administered = new Map(groups.filter(g => id(g.admin) === sender).map(g => [id(g.objectId), g]));
  if (request.command === 'create-deployment') {
    const targetGroups = unique(request.targetGroups), targetDevices = unique(request.targetDevices);
    if (!targetGroups.length && !targetDevices.length) throw Error('Deployment needs targets');
    if (targetGroups.some(g => !administered.has(g)) || targetDevices.some(d => !owned.has(d) || !active.has(d))) throw Error('Target authority or active identity missing');
    if (!request.component || !request.version || !request.recipeBlob || !/^[0-9a-f]{64}$/.test(request.artifactSha256 ?? '') || !/^[0-9a-f]{64}$/.test(request.recipeSha256 ?? '')) throw Error('Recipe blob, 64-character lowercase hex digests and component/version required');
    return buildCreateDeploymentTx(request.component, request.version, request.recipeBlob, request.artifactSha256!, request.recipeSha256!, targetGroups, targetDevices);
  }
  const group = request.group && administered.get(id(request.group));
  if (!group) throw Error('Sender is not the group admin');
  const members = new Set(group.deviceIds.map(value => id(value))), selected = unique(request.devices);
  if (!selected.length) throw Error('No devices requested');
  const tx = new Transaction();
  for (const deviceId of selected) {
    if (request.command === 'remove-devices') {
      if (!members.has(deviceId)) throw Error('Device is not a member');
      calls.device_group_remove_device(tx, PACKAGE_ID, group.objectId, deviceId);
    } else {
      const device = owned.get(deviceId);
      if (!device || members.has(deviceId)) throw Error('Device ownership or membership mismatch');
      if (device.ownerCapId) {
        const [borrowed, loan] = calls.device_registry_borrow_device(tx, PACKAGE_ID, REGISTRY_ID, device.ownerCapId);
        calls.device_group_add_device(tx, PACKAGE_ID, group.objectId, borrowed);
        calls.device_registry_return_device(tx, PACKAGE_ID, REGISTRY_ID, borrowed, loan);
      } else calls.device_group_add_device(tx, PACKAGE_ID, group.objectId, deviceId);
    }
  }
  return tx;
}
export async function buildOperation(client: SuiGrpcClient, request: Operation): Promise<Transaction> {
  const sender = id(request.sender);
  const [devices, groups] = await Promise.all([fetchDevices(client, sender), fetchGroups(client, sender)]);
  const identities = await readIdentities(client, devices.map(d => d.objectId));
  const active = new Set(devices.filter((_, index) => identities[index] && !identities[index]!.revoked).map(device => id(device.objectId)));
  return planOperation(request, devices, groups, active);
}
