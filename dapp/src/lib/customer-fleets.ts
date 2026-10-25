import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag } from "@mysten/sui/utils";
import { MANIFEST, moveType, normalizeId, verifyDeployment } from "./deployment-manifest";
import { PACKAGE_ID, REGISTRY_ID } from "./constants";
import { configuredDataFleets, readFleetData } from "./fleet-data";
import { hasScopedReadGrant } from "./opaque-data";
import { allOwned, allEvents } from "./sui";
import { customer_CustomerBcs, customer_FleetBcs, customer_MembershipBcs, customer_FleetCreatedBcs } from "./fleet-protocol";

export const CUSTOMER_TYPES = ["customer::Customer", "customer::Fleet", "customer::Membership"] as const;
export function customersSupported() { return !!MANIFEST && CUSTOMER_TYPES.every(type => type in MANIFEST!.type_origins); }
export interface NamedFleet {
  id: string; customerId: string; customerName: string; name: string; budgetId: string;
  support?: boolean;
  devices: string[]; groups: string[]; deployments: string[]; administrator: boolean; isDefault: boolean;
  credentials: { wallet: string; administrator: boolean }[];
  enrollmentPolicies: string[];
  policies: { id: string; capId: string }[];
  activeDevices: bigint; deviceLimit: bigint; allowance: bigint; used: bigint;
}
export async function fetchCustomerFleets(client: SuiGrpcClient, wallet: string): Promise<NamedFleet[]> {
  await verifyDeployment(client);
  if (!customersSupported()) return [];
  const credentials = await allOwned(client, wallet, moveType("customer::Membership"));
  const ids = [...new Set(credentials.map(o => customer_MembershipBcs.parse(o.content).customer_id))];
  const supportBudgets = new Set<string>();
  const readerBudgets = new Set<string>();
  for (const config of configuredDataFleets()) {
    const data = await readFleetData(client, config);
    if (data.support.some(address => normalizeId(address) === normalizeId(wallet))) supportBudgets.add(normalizeId(config.budgetId));
    else if (await hasScopedReadGrant(client, config, wallet)) readerBudgets.add(normalizeId(config.budgetId));
  }
  if (supportBudgets.size || readerBudgets.size) {
    const events = await allEvents(client, moveType("customer::FleetCreated"));
    for (const event of events) {
      const created = customer_FleetCreatedBcs.parse(event.bcs);
      if ((supportBudgets.has(normalizeId(created.budget_id)) || readerBudgets.has(normalizeId(created.budget_id))) && !ids.includes(created.customer_id)) ids.push(created.customer_id);
    }
  }
  const result: NamedFleet[] = [];
  for (const id of ids) {
    const { object } = await client.getObject({ objectId: id, include: { content: true } });
    if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("customer::Customer"))) throw Error("Unexpected customer type");
    const c = customer_CustomerBcs.parse(object.content);
    if (BigInt(c.version) !== BigInt(1)) throw Error("Customer authority version requires a newer dashboard");
    if (normalizeId(c.id) !== normalizeId(id) || normalizeId(c.registry_id) !== normalizeId(REGISTRY_ID)) throw Error("Customer registry mismatch");
    const credential = c.credentials.contents.find(entry => normalizeId(entry.key) === normalizeId(wallet));
    if (!credential && !supportBudgets.size && !readerBudgets.size) continue; // Owned credentials are hints; current shared authority is decisive.
    for (const fleetId of c.fleets) {
      const { object: fleet } = await client.getObject({ objectId: fleetId, include: { content: true } });
      if (normalizeStructTag(fleet.type) !== normalizeStructTag(moveType("customer::Fleet"))) throw Error("Unexpected fleet type");
      const f = customer_FleetBcs.parse(fleet.content);
      if (BigInt(f.version) !== BigInt(1)) throw Error("Fleet authority version requires a newer dashboard");
      if (normalizeId(f.id) !== normalizeId(fleetId) || normalizeId(f.customer_id) !== normalizeId(id)) throw Error("Fleet customer mismatch");
      const support = supportBudgets.has(normalizeId(f.budget_id));
      if (!credential && !support && !readerBudgets.has(normalizeId(f.budget_id))) continue;
      result.push({ support, id: fleetId, customerId: id, customerName: c.name, name: f.name, budgetId: f.budget_id,
        credentials: c.credentials.contents.map(c => ({ wallet: c.key, administrator: c.value })), enrollmentPolicies: f.enrollment_policies, devices: f.devices, groups: f.groups, deployments: f.deployments, policies: f.policies.contents.map(p => ({ id: p.key, capId: p.value })), administrator: credential?.value ?? false,
        isDefault: c.default_fleet === fleetId, activeDevices: BigInt(c.active_devices), deviceLimit: BigInt(c.device_limit),
        allowance: BigInt(c.allowance), used: BigInt(c.used) });
    }
  }
  return result;
}
export function buildCreateCustomerTx(name: string) {
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::create`, arguments: [tx.object(REGISTRY_ID), tx.pure.string(name)] });
  return tx;
}
export function buildCreateFleetTx(customerId: string, name: string) {
  if (!name.trim() || new TextEncoder().encode(name).length > 80) throw Error("Enter a fleet name of 1–80 bytes");
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::create_fleet`, arguments: [tx.object(customerId), tx.object(REGISTRY_ID), tx.pure.string(name.trim())] });
  return tx;
}
export function selectionKey(wallet: string) {
  return `suifleet:selection:${MANIFEST?.chain_identifier}:${REGISTRY_ID}:${normalizeId(wallet)}`;
}
export function selectFleet(fleets: NamedFleet[], preferred: string | null): NamedFleet | undefined {
  // A stale explicit selection is never silently substituted.
  return preferred ? fleets.find(f => f.id === preferred) : fleets.find(f => f.isDefault) ?? fleets[0];
}

/** Compose the existing reviewed budget/storage/settings builders with a
 * transaction-scoped capability loan. The hot potato prevents cap retention.
 * All result/input indices are remapped, including nested arguments. */
export function withManagedBudgetCap(original: Transaction, fleet: NamedFleet, capId: string): Transaction {
  const tx = Transaction.from(original);
  const initial = tx.getData();
  const capIndex = initial.inputs.findIndex(input => {
    const id = input.UnresolvedObject?.objectId ?? input.Object?.ImmOrOwnedObject?.objectId;
    return id && normalizeId(id) === normalizeId(capId);
  });
  if (capIndex < 0) return original;
  const n = initial.commands.length;
  const [cap, loan] = tx.moveCall({ target: `${PACKAGE_ID}::customer::borrow_budget_cap`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id)] });
  tx.moveCall({ target: `${PACKAGE_ID}::customer::return_budget_cap`, arguments: [tx.object(fleet.id), cap, loan] });
  const data = tx.getData();
  function remap(value: unknown): unknown {
    if (Array.isArray(value)) return value.map(remap);
    if (!value || typeof value !== "object") return value;
    if ("Input" in value && typeof value.Input === "number") {
      if (value.Input === capIndex) return { NestedResult: [0, 0] };
      return { ...value, Input: value.Input > capIndex ? value.Input - 1 : value.Input };
    }
    const index = (i: number) => i === n ? 0 : i < n ? i + 1 : i;
    if ("Result" in value && typeof value.Result === "number") return { ...value, Result: index(value.Result) };
    if ("NestedResult" in value && Array.isArray(value.NestedResult)) return { ...value, NestedResult: [index(value.NestedResult[0]), value.NestedResult[1]] };
    return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, remap(child)]));
  }
  return Transaction.from(JSON.stringify({ ...data, inputs: data.inputs.filter((_, i) => i !== capIndex),
    commands: [data.commands[n], ...data.commands.slice(0, n), data.commands[n + 1]].map(remap) }));
}

export async function buildDeregisterManagedDeviceTx(client: SuiGrpcClient, fleet: NamedFleet, deviceId: string) {
  const { fetchAccount } = await import("./fleet-budget");
  const account = await fetchAccount(client, fleet.budgetId, deviceId);
  if (!account || !fleet.devices.includes(deviceId)) throw Error("Device is not enrolled in this fleet");
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::deregister_device`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(fleet.budgetId), tx.object(account.accountId), tx.object(REGISTRY_ID), tx.pure.id(deviceId)] });
  return tx;
}
export function buildCredentialTx(customerId: string, wallet: string, active: boolean, administrator: boolean) {
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::set_credential`, arguments: [tx.object(customerId), tx.pure.address(normalizeId(wallet)), tx.pure.bool(active), tx.pure.bool(administrator)] });
  return tx;
}
export async function buildReassignDeviceTx(client: SuiGrpcClient, source: NamedFleet, destination: NamedFleet, deviceId: string) {
  if (source.customerId !== destination.customerId || source.id === destination.id) throw Error("Choose another fleet of this customer");
  const { fetchAccount } = await import("./fleet-budget");
  const account = await fetchAccount(client, source.budgetId, deviceId);
  if (!account || !source.devices.includes(deviceId)) throw Error("Device is not enrolled in the source fleet");
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::reassign_device`, arguments: [tx.object(source.customerId), tx.object(source.id), tx.object(destination.id), tx.object(source.budgetId), tx.object(destination.budgetId), tx.object(account.accountId), tx.object(REGISTRY_ID), tx.pure.id(deviceId), tx.object("0x6")] });
  return tx;
}
