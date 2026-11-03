import type { DeviceObject } from "./sui";
import type { NamedFleet } from "./customer-fleets";
// Seal access policies (SF-022): who may obtain decryption keys for a
// release. Granting decryption never grants publish, deploy or admin
// authority; those stay with their own Move capabilities and roles.
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { Transaction, type TransactionArgument } from "@mysten/sui/transactions";
import { normalizeStructTag } from "@mysten/sui/utils";
import { calls, seal_policy_AccessAdminCapBcs as CapBcs, seal_policy_AccessPolicyBcs as PolicyBcs } from "./fleet-protocol";
import { moveType, normalizeId, requireManifest, SEAL_POLICY_TYPES, verifyDeployment } from "./deployment-manifest";
import { allOwned } from "./sui";

/** The policy code version this client builds approvals for. */
export const POLICY_VERSION = "1";

export interface AccessPolicy {
  objectId: string;
  fleet?: NamedFleet;
  capId: string;
  name: string;
  version: string;
  keyEpoch: string;
  accounts: string[];
  devices: string[];
  groups: string[];
}

export type Scope = "account" | "device" | "group";

/** Managed inventory is registered in custody; its owner caps live in the
 * fleet, not the wallet. Decryption grants still require policy authority. */
export function deviceGrantReason(policy: AccessPolicy, device: DeviceObject): string | undefined {
  if (policy.fleet) {
    if (!policy.fleet.administrator) return "Requires a customer administrator credential";
    if (!policy.fleet.devices.some(id => normalizeId(id) === normalizeId(device.objectId))) return "Device is not registered in this fleet";
    return undefined;
  }
  if (!device.ownerCapId) return "Device must be activated in Devices first";
  return undefined;
}

/** The call package, once the manifest enables access policies. */
export function policyPackage(): string {
  const m = requireManifest();
  for (const name of SEAL_POLICY_TYPES) if (!m.type_origins[name]) throw new Error("This deployment manifest does not enable Seal access policies");
  return m.call_package_id;
}

/** Seal's key namespace: the fleet package's original (first-version) ID. */
export function sealNamespace(): string {
  return normalizeId(requireManifest().type_origins["device_registry::DeviceRegistry"]);
}

/** Policies the account administers (holds the AccessAdminCap of). */
export async function fetchAdministeredPolicies(client: SuiGrpcClient, owner: string): Promise<AccessPolicy[]> {
  policyPackage();
  await verifyDeployment(client);
  const caps = await allOwned(client, owner, moveType("seal_policy::AccessAdminCap"));
  return Promise.all(caps.map(async cap => {
    const { policy_id } = CapBcs.parse(cap.content);
    const { object } = await client.getObject({ objectId: policy_id, include: { content: true } });
    if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("seal_policy::AccessPolicy")) || object.owner.$kind !== "Shared") {
      throw new Error(`Access policy ${policy_id} is not a shared SuiFleet policy`);
    }
    const p = PolicyBcs.parse(object.content);
    return {
      objectId: normalizeId(policy_id), capId: cap.objectId, name: p.name, version: String(p.version), keyEpoch: String(p.key_epoch),
      accounts: p.accounts.map(normalizeId), devices: p.devices.map(normalizeId), groups: p.groups.map(normalizeId),
    };
  }));
}

export async function fetchFleetPolicies(client: SuiGrpcClient, fleet: NamedFleet): Promise<AccessPolicy[]> {
  return Promise.all(fleet.policies.map(async ({ id, capId }) => {
    const { object } = await client.getObject({ objectId: id, include: { content: true } });
    if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("seal_policy::AccessPolicy")) || object.owner.$kind !== "Shared") throw Error("Unexpected fleet access policy");
    const p = PolicyBcs.parse(object.content);
    return { objectId: normalizeId(id), capId, fleet, name: p.name, version: String(p.version), keyEpoch: String(p.key_epoch),
      accounts: p.accounts.map(normalizeId), devices: p.devices.map(normalizeId), groups: p.groups.map(normalizeId) };
  }));
}
function managedChange(tx: Transaction, policy: AccessPolicy, scope: Scope, subject: string, grant: boolean) {
  const fleet = policy.fleet!;
  if (scope === "group" && !grant) {
    tx.moveCall({ target: `${policyPackage()}::customer::revoke_policy_group`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(policy.objectId), tx.pure.id(subject)] }); return;
  }
  const args: TransactionArgument[] = [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(policy.objectId)];
  if (scope === "device") args.push(tx.object(requireManifest().shared_objects.registry.id));
  args.push(scope === "group" ? tx.object(subject) : scope === "account" ? tx.pure.address(subject) : tx.pure.id(subject), tx.pure.bool(grant));
  tx.moveCall({ target: `${policyPackage()}::customer::${({account: "policy_account", device: "policy_device", group: "policy_group"} as const)[scope]}`, arguments: args });
}

export function buildCreatePolicyTx(name: string, admin: string, fleet?: NamedFleet): Transaction {
  if (!name.trim() || name.length > 128) throw new Error("Policy name must be 1 to 128 characters");
  const tx = new Transaction();
  if (fleet) { tx.moveCall({ target: `${policyPackage()}::customer::create_policy`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.pure.string(name.trim())] }); return tx; }
  const cap = calls.seal_policy_create_policy(tx, policyPackage(), name.trim());
  tx.transferObjects([cap], admin);
  return tx;
}

export function buildGrantTx(policy: AccessPolicy, scope: Scope, subject: string): Transaction {
  const tx = new Transaction();
  const pkg = policyPackage();
  const id = normalizeId(subject);
  if (policy.fleet) { managedChange(tx, policy, scope, id, true); return tx; }
  if (scope === "account") calls.seal_policy_grant_account(tx, pkg, policy.objectId, policy.capId, id);
  else if (scope === "device") calls.seal_policy_grant_device(tx, pkg, policy.objectId, policy.capId, requireManifest().shared_objects.registry.id, id);
  else calls.seal_policy_grant_group(tx, pkg, policy.objectId, policy.capId, id);
  return tx;
}

export function buildRevokeTx(policy: AccessPolicy, scope: Scope, subject: string): Transaction {
  const tx = new Transaction();
  const pkg = policyPackage();
  const id = normalizeId(subject);
  if (policy.fleet) { managedChange(tx, policy, scope, id, false); return tx; }
  if (scope === "account") calls.seal_policy_revoke_account(tx, pkg, policy.objectId, policy.capId, id);
  else if (scope === "device") calls.seal_policy_revoke_device(tx, pkg, policy.objectId, policy.capId, id);
  else calls.seal_policy_revoke_group(tx, pkg, policy.objectId, policy.capId, id);
  return tx;
}

/** Retire every identity of the current epoch: deployments encrypted under it can no longer be decrypted by anyone. */
export function buildRotateTx(policy: AccessPolicy): Transaction {
  const tx = new Transaction();
  if (policy.fleet) { tx.moveCall({ target: `${policyPackage()}::customer::rotate_policy`, arguments: [tx.object(policy.fleet.customerId), tx.object(policy.fleet.id), tx.object(policy.objectId)] }); return tx; }
  calls.seal_policy_rotate_key_epoch(tx, policyPackage(), policy.objectId, policy.capId);
  return tx;
}
