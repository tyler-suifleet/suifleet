import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag } from "@mysten/sui/utils";
import { PACKAGE_ID, REGISTRY_ID } from "./constants";
import { moveType, normalizeId, verifyDeployment } from "./deployment-manifest";
import { customer_CustomerBcs, device_registry_DeviceRegistryBcs } from "./fleet-protocol";

export function deviceCapacityMessage(activeDevices: bigint, deviceLimit: bigint) {
  return `Device limit reached: ${activeDevices} active devices, limit ${deviceLimit} across this customer’s fleets. Ask the registry administrator to raise the limit in Administration → Device entitlements. Adding network funds does not raise this limit.`;
}
export async function readCustomerEntitlement(client: SuiGrpcClient, customerId: string) {
  await verifyDeployment(client);
  const { object } = await client.getObject({ objectId: normalizeId(customerId), include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("customer::Customer"))) throw Error("Choose a customer account object");
  const c = customer_CustomerBcs.parse(object.content);
  if (normalizeId(c.id) !== normalizeId(customerId) || normalizeId(c.registry_id) !== normalizeId(REGISTRY_ID) || BigInt(c.version) !== BigInt(1)) throw Error("Customer does not belong to this deployment or requires a newer console");
  return c;
}
export async function registryAdministrator(client: SuiGrpcClient) {
  await verifyDeployment(client);
  const { object } = await client.getObject({ objectId: REGISTRY_ID, include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("device_registry::DeviceRegistry"))) throw Error("Unexpected registry type");
  return normalizeId(device_registry_DeviceRegistryBcs.parse(object.content).admin);
}
export function buildGrantDeviceLimitTx(customerId: string, limit: string, current: Awaited<ReturnType<typeof readCustomerEntitlement>>) {
  if (!/^\d+$/.test(limit) || BigInt(limit) > BigInt(18446744073709551615)) throw Error("Enter a whole device limit between 0 and 18446744073709551615");
  if (BigInt(limit) < BigInt(current.active_devices)) throw Error("The limit cannot be below the active device count");
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::set_entitlement`, arguments: [tx.object(customerId), tx.object(REGISTRY_ID), tx.pure.u64(limit), tx.pure.u64(current.allowance), tx.pure.u64(current.period)] });
  return tx;
}
/** Fresh state immediately before approval, including multi-registration PTBs. */
export async function checkDeviceCapacity(client: SuiGrpcClient, transaction: Transaction) {
  const data = transaction.getData();
  const additions = new Map<string, bigint>();
  for (const command of data.commands) {
    const call = command.MoveCall;
    if (!call || normalizeId(call.package) !== normalizeId(PACKAGE_ID) || call.module !== "customer" || !["register_device", "register_funded_device", "enroll", "enroll_claim"].includes(call.function)) continue;
    const arg = call.arguments[0];
    const input = "Input" in arg ? data.inputs[arg.Input] : undefined;
    const id = input?.UnresolvedObject?.objectId ?? input?.Object?.SharedObject?.objectId ?? input?.Object?.ImmOrOwnedObject?.objectId;
    if (!id) throw Error("Cannot verify the customer device limit; refresh and retry");
    additions.set(id, (additions.get(id) ?? BigInt(0)) + BigInt(1));
  }
  for (const [id, count] of additions) {
    const c = await readCustomerEntitlement(client, id);
    if (BigInt(c.active_devices) + count > BigInt(c.device_limit)) throw Error(deviceCapacityMessage(BigInt(c.active_devices), BigInt(c.device_limit)));
  }
}
export function registrationFailure(message: string) {
  if (/MoveAbort/i.test(message)) return "Registration was refused by the network. Refresh the fleet to check its device limit and permissions. Ask the registry administrator to review Device entitlements if capacity is full.";
  return message;
}
