import { fetchCustomerFleets, customersSupported, type NamedFleet } from "./customer-fleets";
// Operations use the same verified chain readers and local builders as the dApp.
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction } from '@mysten/sui/transactions';
import { normalizeSuiAddress as id } from '@mysten/sui/utils';
import { calls } from './fleet-protocol';
import { PACKAGE_ID, REGISTRY_ID } from './constants';
import { fetchDevices, fetchFleetDevices, fetchGroups, buildCreateDeploymentTx, type DeviceObject, type DeviceGroup } from './sui';
import { readIdentities } from './telemetry';
import { announcementsFor, existingInboxes, inboxId, inboxSupported, withInbox } from './fleet-inbox';

export interface Operation {
  command: 'add-devices' | 'remove-devices' | 'create-deployment';
  sender: string;
  fleetId?: string;
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
/**
 * Pure intent planning, separated from chain reads for independent call checks.
 * With inbox origins (SF-125), `inboxes` lists the requested devices whose
 * inbox exists: membership changes then list/unlist the group in the inbox
 * and deployments announce to group heads and device inboxes.
 */
export function planOperation(request: Operation, devices: DeviceObject[], groups: DeviceGroup[], active: Set<string>, inboxes = new Set<string>(), fleet?: NamedFleet): Transaction {
  const inbox = inboxSupported();
  if (!['add-devices', 'remove-devices', 'create-deployment'].includes(request.command)) throw Error('Unknown operation');
  const sender = id(request.sender);
  const owned = new Map(devices.map(d => [id(d.objectId), d]));
  const administered = new Map(groups.filter(g => id(g.admin) === (fleet?.id ?? sender)).map(g => [id(g.objectId), g]));
  if (request.command === 'create-deployment') {
    const targetGroups = unique(request.targetGroups), targetDevices = unique(request.targetDevices);
    if (!targetGroups.length && !targetDevices.length) throw Error('Deployment needs targets');
    if (targetGroups.some(g => !administered.has(g)) || targetDevices.some(d => !owned.has(d) || !active.has(d))) throw Error('Target authority or active identity missing');
    if (!request.component || !request.version || !request.recipeBlob || !/^[0-9a-f]{64}$/.test(request.artifactSha256 ?? '') || !/^[0-9a-f]{64}$/.test(request.recipeSha256 ?? '')) throw Error('Recipe blob, 64-character lowercase hex digests and component/version required');
    return buildCreateDeploymentTx(request.component, request.version, request.recipeBlob, request.artifactSha256!, request.recipeSha256!, targetGroups, targetDevices, false,
      fleet ? undefined : announcementsFor(groups, devices, sender, targetGroups, targetDevices, inboxes), fleet);
  }
  const group = request.group && administered.get(id(request.group));
  if (!group) throw Error('Sender is not the group admin');
  const members = new Set(group.deviceIds.map(value => id(value))), selected = unique(request.devices);
  if (!selected.length) throw Error('No devices requested');
  const tx = new Transaction();
  for (const deviceId of selected) {
    if (request.command === 'remove-devices') {
      if (!members.has(deviceId)) throw Error('Device is not a member');
      if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::remove_from_group`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(group.objectId), tx.pure.id(deviceId)] });
      else if (inbox && inboxes.has(deviceId)) calls.fleet_inbox_remove_member(tx, PACKAGE_ID, group.objectId, inboxId(deviceId));
      else calls.device_group_remove_device(tx, PACKAGE_ID, group.objectId, deviceId);
    } else {
      const device = owned.get(deviceId);
      if (!device || members.has(deviceId)) throw Error('Device ownership or membership mismatch');
      const ownerCapId = device.ownerCapId;
      if (fleet) tx.moveCall({ target: `${PACKAGE_ID}::customer::add_to_group`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(group.objectId), tx.pure.id(deviceId)] });
      else if (ownerCapId) {
        // A missing inbox is created before the custody loan (new_inbox needs custody).
        const add = (inboxArg?: Parameters<typeof calls.fleet_inbox_add_member>[3]) => {
          const [borrowed, loan] = calls.device_registry_borrow_device(tx, PACKAGE_ID, REGISTRY_ID, ownerCapId);
          if (inboxArg === undefined) calls.device_group_add_device(tx, PACKAGE_ID, group.objectId, borrowed);
          else calls.fleet_inbox_add_member(tx, PACKAGE_ID, group.objectId, inboxArg, borrowed);
          calls.device_registry_return_device(tx, PACKAGE_ID, REGISTRY_ID, borrowed, loan);
        };
        if (inbox) withInbox(tx, { deviceId, ownerCapId, exists: inboxes.has(deviceId) }, add);
        else add();
      } else calls.device_group_add_device(tx, PACKAGE_ID, group.objectId, deviceId);
    }
  }
  return tx;
}
export async function buildOperation(client: SuiGrpcClient, request: Operation): Promise<Transaction> {
  const sender = id(request.sender);
  const fleets = customersSupported() ? await fetchCustomerFleets(client, sender) : [];
  if (fleets.length && !request.fleetId) throw Error("Specify fleetId for a managed customer operation");
  const fleet = request.fleetId ? fleets.find(f => f.id === id(request.fleetId!)) : undefined;
  if (request.fleetId && !fleet?.administrator) throw Error("Fleet administrator authority is missing");
  const [devices, groups] = await Promise.all([fleet ? fetchFleetDevices(client, fleet.devices) : fetchDevices(client, sender), fetchGroups(client, fleet?.id ?? sender)]);
  const identities = await readIdentities(client, devices.map(d => d.objectId));
  const active = new Set(devices.filter((_, index) => identities[index] && !identities[index]!.revoked).map(device => id(device.objectId)));
  const requested = [...(request.devices ?? []), ...(request.targetDevices ?? [])].map(value => id(value));
  const inboxes = !fleet && inboxSupported() ? await existingInboxes(client, [...new Set(requested)]) : new Set<string>();
  return planOperation(request, devices, groups, active, inboxes, fleet);
}
