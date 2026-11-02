import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { bcs } from "@mysten/sui/bcs";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag } from "@mysten/sui/utils";
import { blake2b } from "@noble/hashes/blake2.js";
import { MANIFEST, SECURITY_DELAY_TYPES, moveType, verifyDeployment } from "./deployment-manifest";
import { PACKAGE_ID } from "./constants";
import { incrementalEvents } from "./event-history";
import * as wire from "./fleet-protocol";

export const MIN_DELAY_MS = 1000;
export const MAX_DELAY_MS = 518400000;
export function delaySupported() { return !!MANIFEST && SECURITY_DELAY_TYPES.every(t => t in MANIFEST!.type_origins); }
export function validateDelays(values: readonly number[]) {
  if (values.length !== 7 || values.some(v => !Number.isSafeInteger(v) || v < MIN_DELAY_MS || v > MAX_DELAY_MS)) throw Error("Choose seven delays between 1 second and 6 days");
  return [...values];
}
const vectors = bcs.vector(bcs.vector(bcs.u8()));
const durations = bcs.vector(bcs.u64());
const requirements = bcs.vector(bcs.u8());
export type SecurityEdit = { operation: "set_security" | "configure_security_delays"; values: number[] };
export interface QueuedSecurityAction { id: string; proposer: string; generation: bigint; expires: bigint; created?: bigint; intent: number[]; edit?: SecurityEdit; readyAt?: bigint; stale: boolean }
function requireDelay() { if (!delaySupported()) throw Error("Deploy the delay mechanism and regenerate the verified manifest first"); }
export function buildDelayConfigurationTx(customer: string, values: readonly number[]) {
  requireDelay(); const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::configure_security_delays`, arguments: [tx.object(customer), tx.pure(durations.serialize(validateDelays(values)).toBytes())] });
  return tx;
}
// Shared mutable targets bind their IDs; never encode counters or balances.
export function targetIdentity(id: string): Uint8Array { return bcs.Address.serialize(id).toBytes(); }
export function editArguments(customerBytes: Uint8Array, edit: SecurityEdit): Uint8Array {
  const values = edit.operation === "configure_security_delays" ? durations.serialize(validateDelays(edit.values)).toBytes() : requirements.serialize(edit.values).toBytes();
  return vectors.serialize([bcs.string().serialize(edit.operation).toBytes(), targetIdentity(wire.customer_CustomerBcs.parse(customerBytes).id), values]).toBytes();
}
export function securityIntent(customer: string, argumentsBcs: Uint8Array): number[] {
  // Move's TypeName is its address without 0x, followed by module and type.
  const typeName = moveType("security_policy::Action").slice(2);
  return Array.from(blake2b(vectors.serialize([bcs.string().serialize(typeName).toBytes(), bcs.Address.serialize(customer).toBytes(), argumentsBcs]).toBytes(), { dkLen: 32 }));
}
export function buildQueueSecurityTx(customer: string, argumentsBcs: Uint8Array, expires: bigint) {
  requireDelay(); if (argumentsBcs.length > 65536) throw Error("Action arguments exceed 64 KiB");
  const tx = new Transaction();
  tx.moveCall({ target: `${PACKAGE_ID}::customer::queue_security_action`, arguments: [tx.object(customer), tx.pure.vector("u8", Array.from(argumentsBcs)), tx.pure.u64(expires), tx.object("0x6")] });
  return tx;
}
export function buildCancelSecurityTx(customer: string, action: string) {
  const tx = new Transaction(); tx.moveCall({ target: `${PACKAGE_ID}::customer::cancel_security_action`, arguments: [tx.object(customer), tx.object(action)] }); return tx;
}
export function buildExecuteSecurityEditTx(customer: string, action: string, edit: SecurityEdit) {
  requireDelay(); const tx = new Transaction();
  const values = edit.operation === "set_security" ? tx.pure.vector("u8", edit.values) : tx.pure(durations.serialize(validateDelays(edit.values)).toBytes());
  tx.moveCall({ target: `${PACKAGE_ID}::customer::${edit.operation}_secured`, arguments: [tx.object(customer), values, tx.object(action), tx.object("0x6")] }); return tx;
}
export async function currentCustomerBytes(client: SuiGrpcClient, customer: string) {
  await verifyDeployment(client);
  const { object } = await client.getObject({ objectId: customer, include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("customer::Customer"))) throw Error("Unexpected organization type");
  return object.content;
}
async function optionalField(client: SuiGrpcClient, parentId: string, name: typeof SECURITY_DELAY_TYPES[number]) {
  try {
    const field = await client.getDynamicField({ parentId, name: { type: moveType(name), bcs: bcs.bool().serialize(false).toBytes() } });
    return field.dynamicField.value;
  } catch (error) { if (error instanceof ObjectError && error.reason === "notFound") return undefined; throw error; }
}
export async function fetchSecurityDelays(client: SuiGrpcClient, customer: string): Promise<number[] | undefined> {
  await verifyDeployment(client); requireDelay();
  const field = await optionalField(client, customer, "security_policy::DelayKey");
  if (!field) return undefined;
  if (normalizeStructTag(field.type) !== normalizeStructTag(moveType("security_policy::Delays"))) throw Error("Unexpected delay configuration type");
  return validateDelays(wire.security_policy_DelaysBcs.parse(field.bcs).milliseconds.map(Number));
}
export function delayStatus(action: QueuedSecurityAction, now: bigint): string {
  if (action.stale) return "Invalidated by organization state or policy change";
  if (now >= action.expires) return "Expired";
  if (action.readyAt === undefined) return "Timing unavailable; cancel and queue again";
  return now < action.readyAt ? `Ready in ${((action.readyAt - now + BigInt(999)) / BigInt(1000)).toString()} seconds` : "Ready to execute";
}
export async function fetchQueuedSecurityActions(client: SuiGrpcClient, customer: string, generation: bigint, policy: readonly number[], delays: readonly number[]) {
  await verifyDeployment(client); requireDelay();
  const events = await incrementalEvents(client, moveType("security_policy::ActionCreated"));
  const ids = [...new Set(events.map(e => wire.security_policy_ActionCreatedBcs.parse(e.bcs)).filter(e => e.customer_id === customer).map(e => e.action_id))];
  const current = bcs.Address.serialize(wire.customer_CustomerBcs.parse(await currentCustomerBytes(client, customer)).id).toBytes();
  const result: QueuedSecurityAction[] = [];
  for (let offset = 0; offset < ids.length; offset += 50) {
    const { objects } = await client.getObjects({ objectIds: ids.slice(offset, offset + 50), include: { content: true } });
    for (const object of objects) {
      if (object instanceof ObjectError && ["notFound", "deleted"].includes(object.reason)) continue;
      if (object instanceof Error) throw object;
      if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("security_policy::Action"))) throw Error("Unexpected queued action type");
      const a = wire.security_policy_ActionBcs.parse(object.content);
      if (a.customer_id !== customer) throw Error("Queued action organization mismatch");
      const timestamp = await optionalField(client, a.id, "security_policy::QueuedAtKey");
      if (timestamp && normalizeStructTag(timestamp.type) !== normalizeStructTag(moveType("security_policy::QueuedAt"))) throw Error("Unexpected queue timestamp type");
      const created = timestamp ? BigInt(wire.security_policy_QueuedAtBcs.parse(timestamp.bcs).milliseconds) : undefined;
      const payload = await optionalField(client, a.id, "security_policy::ArgumentsKey");
      let edit: SecurityEdit | undefined;
      let stateChanged = false;
      if (payload) {
        if (normalizeStructTag(payload.type) !== normalizeStructTag(moveType("security_policy::ActionArguments"))) throw Error("Unexpected action arguments type");
        const bytes = Uint8Array.from(wire.security_policy_ActionArgumentsBcs.parse(payload.bcs).bytes);
        if (securityIntent(customer, bytes).some((v, i) => v !== a.intent[i])) throw Error("Queued action intent mismatch");
        try {
        const args = vectors.parse(bytes);
        if (args.length >= 1) {
          const name = bcs.string().parse(Uint8Array.from(args[0]));
          if (args.length === 3 && (name === "set_security" || name === "configure_security_delays")) {
            const values = name === "set_security" ? requirements.parse(Uint8Array.from(args[2])) : durations.parse(Uint8Array.from(args[2])).map(Number);
            stateChanged = args[1].length !== 32 || args[1].some((v, i) => v !== current[i]);
            edit = { operation: name, values };
          }
        }
        } catch { /* Opaque canonical intents remain cancellable. */ }
      }
      // Settings edits require the union of classes. Unknown operations retain a
      // conservative longest-delay countdown and are executed in their own flow.
      const delay = Math.max(0, ...policy.map((mask, i) => mask & 8 ? delays[i] ?? MAX_DELAY_MS : 0));
      result.push({ id: a.id, proposer: a.proposer, intent: a.intent, generation: BigInt(a.generation), expires: BigInt(a.expires_ms), created, edit, readyAt: created === undefined ? undefined : created + BigInt(delay), stale: BigInt(a.generation) !== generation || stateChanged });
    }
  }
  return result;
}
