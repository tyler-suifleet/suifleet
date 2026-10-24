import { bcs, TypeTagSerializer } from "@mysten/sui/bcs";
import { ObjectError } from "@mysten/sui/client";
import { moveType, requireManifest } from "./deployment-manifest";
import { device_registry_IdentityBcs } from "./fleet-protocol";
import { deriveDynamicFieldID, normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import type { Transaction } from "@mysten/sui/transactions";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { buildEnrollDevicesTx, fetchAccounts, fetchBudgets, type BudgetView } from "./fleet-budget";

// Leave headroom below the 256 input-object limit and 128 KiB transaction limit.
export const ENROLLMENT_CHUNK_SIZE = 200;
export const ENROLLMENT_MAX_BYTES = 120_000;
export const ENROLLMENT_MAX_GAS = BigInt(2_000_000_000);
export interface EnrollmentDevice { objectId: string; ownerCapId: string }
export interface EnrollmentProgress {
  confirmed: number; skipped: number; total: number; chunk: number;
  excluded: { deviceId: string; reason: string }[];
  digests: string[]; phase: "checking" | "approval" | "confirmed" | "complete" | "stopped";
  estimatedGas?: string;
}

/** Build resolves inputs and simulates for gas before any wallet approval. */
export async function prepareEnrollment(tx: Transaction, client: SuiGrpcClient, sender: string) {
  tx.setSender(sender);
  const bytes = await tx.build({ client });
  if (bytes.length > ENROLLMENT_MAX_BYTES) throw new Error("Enrollment chunk exceeds the transaction size limit");
  const gas = BigInt(tx.getData().gasData.budget ?? 0);
  if (gas <= BigInt(0) || gas > ENROLLMENT_MAX_GAS) throw new Error("Enrollment chunk exceeds the 2 SUI gas ceiling");
  return gas.toString();
}

/** Always re-read the chain, including after an ambiguous previous submission.
 * A failed/uncertain chunk stops the run. Atomic PTBs never partially enroll.
 * Reads can lag a committed transaction: a stale retry then aborts atomically
 * on AlreadyEnrolled rather than paying another float. No local success cache.
 */
export async function enrollBudgetDevices(options: {
  devices: EnrollmentDevice[];
  readBudget: () => Promise<BudgetView>;
  readStates: (devices: EnrollmentDevice[]) => Promise<(string | undefined)[]>;
  prepare: (tx: Transaction) => Promise<string>;
  execute: (tx: Transaction) => Promise<{ digest: string }>;
  onProgress: (progress: EnrollmentProgress) => void;
}) {
  let devices = [...options.devices];
  const seen = new Set<string>();
  for (const device of devices) {
    const key = normalizeSuiAddress(device.objectId), cap = normalizeSuiAddress(device.ownerCapId);
    if (seen.has(key) || seen.has(cap)) throw new Error("Duplicate device or owner cap in enrollment selection");
    seen.add(key); seen.add(cap);
  }
  const progress: EnrollmentProgress = { confirmed: 0, skipped: 0, total: devices.length, chunk: 0, digests: [], excluded: [], phase: "checking" };
  const report = (phase: EnrollmentProgress["phase"]) => {
    progress.phase = phase;
    options.onProgress({ ...progress, digests: [...progress.digests], excluded: [...progress.excluded] });
  };
  const pendingDevices = async (group: EnrollmentDevice[]) => {
    const states = await options.readStates(group);
    if (states.length !== group.length) throw new Error("Incomplete enrollment states");
    return group.filter((device, i) => {
      if (states[i] === "enrolled") { progress.skipped++; return false; }
      if (states[i]) { progress.excluded.push({ deviceId: device.objectId, reason: states[i]! }); return false; }
      return true;
    });
  };
  try {
    // Determine the full outstanding set before reserving funds for future
    // chunks. An already funded fleet can resume even with little unallocated SUI.
    const outstanding: EnrollmentDevice[] = [];
    for (let i = 0; i < devices.length; i += ENROLLMENT_CHUNK_SIZE) {
      report("checking");
      const group = devices.slice(i, i + ENROLLMENT_CHUNK_SIZE);
      outstanding.push(...await pendingDevices(group));
    }
    devices = outstanding;
    while (devices.length) {
      report("checking");
      const candidates = devices.splice(0, ENROLLMENT_CHUNK_SIZE);
      const pending = await pendingDevices(candidates);
      if (!pending.length) continue;
      const budget = await options.readBudget();
      const float = budget.defaultLimits.floatCap;
      // Reserve enough for every remaining float; distribute reserves evenly.
      const remaining = BigInt(pending.length + devices.length);
      if (budget.balance < float * remaining) throw new Error("Deposit enough to fund the remaining device floats before resuming");
      const available = (budget.balance - float * remaining) / remaining;
      const twoPeriods = budget.defaultLimits.periodAllowance * BigInt(2);
      const maxReserve = BigInt(1_000_000_000_000); // fleet_budget::MAX_LIMIT_MIST
      const desired = twoPeriods < maxReserve ? twoPeriods : maxReserve;
      const reserve = available < desired ? available : desired;
      const tx = buildEnrollDevicesTx(budget.id, budget.capId, pending.map(d => ({ ownerCapId: d.ownerCapId, floatMist: float, reserveMist: reserve })));
      progress.estimatedGas = await options.prepare(tx);
      progress.chunk++;
      report("approval");
      const result = await options.execute(tx);
      progress.confirmed += pending.length;
      progress.digests.push(result.digest);
      report("confirmed");
    }
    report("complete");
    return progress;
  } catch (error) { report("stopped"); throw error; }
}

export function enrollmentReads(client: SuiGrpcClient, sender: string, budgetId: string) {
  return {
    readBudget: async () => {
      const budget = (await fetchBudgets(client, sender)).find(b => b.id === budgetId);
      if (!budget) throw new Error("Budget no longer belongs to this wallet");
      return budget;
    },
    readStates: async (devices: EnrollmentDevice[]) => {
      const accounts = await fetchAccounts(client, budgetId, devices.map(d => d.objectId));
      const registry = requireManifest().shared_objects.registry.id;
      const identityKey = moveType("device_registry::IdentityKey");
      const custodyKey = `0x2::dynamic_object_field::Wrapper<${moveType("device_registry::CustodyKey")}>`;
      const IdentityField = bcs.struct("EnrollmentIdentityField", {
        id: bcs.Address, name: bcs.Address, value: device_registry_IdentityBcs,
      });
      const CustodyField = bcs.struct("EnrollmentCustodyField", {
        id: bcs.Address, name: bcs.Address, value: bcs.Address,
      });
      const pending = devices.flatMap((device, index) => accounts[index] ? [] : [{ device, index }]);
      const fields = pending.flatMap(({ device }) => [identityKey, custodyKey].map(key => ({
        key, device, id: deriveDynamicFieldID(registry, TypeTagSerializer.parseFromStr(key), bcs.Address.serialize(device.objectId).toBytes()),
      })));
      const values: (ReturnType<typeof IdentityField.parse> | ReturnType<typeof CustodyField.parse> | undefined)[] = [];
      // Keep only one request outstanding; enrollment shares the browser queue
      // with dashboard polling. Never enqueue hundreds of individual reads.
      for (let i = 0; i < fields.length; i += 50) {
        const batch = fields.slice(i, i + 50);
        const { objects } = await client.getObjects({ objectIds: batch.map(f => f.id), include: { content: true } });
        if (objects.length !== batch.length) throw new Error("Incomplete registry batch");
        values.push(...objects.map((object, j) => {
          if (object instanceof ObjectError && object.reason === "notFound") return undefined;
          if (object instanceof Error) throw object;
          const field = batch[j];
          const valueType = field.key === identityKey ? moveType("device_registry::Identity") : "0x2::object::ID";
          if (normalizeStructTag(object.type) !== normalizeStructTag(`0x2::dynamic_field::Field<${field.key},${valueType}>`)) throw new Error("Unexpected registry field type");
          const parsed = field.key === identityKey ? IdentityField.parse(object.content) : CustodyField.parse(object.content);
          if (normalizeSuiAddress(object.objectId) !== normalizeSuiAddress(field.id) || parsed.id !== field.id || normalizeSuiAddress(parsed.name) !== normalizeSuiAddress(field.device.objectId)) throw new Error("Registry field mismatch");
          return parsed;
        }));
      }
      const states: (string | undefined)[] = accounts.map(account => account ? "enrolled" : undefined);
      pending.forEach(({ device, index }, i) => {
        const field = values[2 * i];
        const identity = field?.value;
        if (!identity) states[index] = "No active registry identity";
        else if (typeof identity === "string") throw new Error("Unexpected Identity value");
        else if (identity.revoked) states[index] = "Device identity revoked";
        else if (normalizeSuiAddress(identity.owner_cap) !== normalizeSuiAddress(device.ownerCapId)) states[index] = "Device owner cap changed";
        else if (!values[2 * i + 1]) states[index] = "Device is not in registry custody";
        else if (values[2 * i + 1]!.value !== normalizeSuiAddress(device.objectId)) throw new Error("Custody device mismatch");
      });
      return states;
    },
  };
}
