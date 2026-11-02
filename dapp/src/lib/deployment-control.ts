import type { NamedFleet } from "./customer-fleets";
import { calls, deployment_manager_ControlBcs, deployment_manager_RecurringWindowBcs } from "./fleet-protocol";
import { requireManifest, moveType, RECURRING_WINDOW_TYPES } from "./deployment-manifest";
import { recurringActive, validateRecurring, type RecurringWindow } from "./recurring-window";
import { PACKAGE_ID } from "./constants";
import { Transaction } from "@mysten/sui/transactions";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { bcs } from "@mysten/sui/bcs";
import { normalizeStructTag } from "@mysten/sui/utils";

export interface DeploymentControl {
  revision: string;
  state: number;
  not_before_ms: string;
  expires_ms: string;
  window_start_ms: string;
  window_end_ms: string;
  notice_ms: string;
}
export const immediateControl: DeploymentControl = {
  revision: "0", state: 0, not_before_ms: "0", expires_ms: "0",
  window_start_ms: "0", window_end_ms: "0", notice_ms: "0",
};
export function validateControl(c: DeploymentControl) {
  const times = [c.not_before_ms, c.expires_ms, c.window_start_ms, c.window_end_ms, c.notice_ms];
  for (const value of times) {
    if (!/^\d+$/.test(value) || BigInt(value) > (BigInt(1) << BigInt(64)) - BigInt(1)) throw new Error("Invalid UTC timestamp or notice duration");
  }
  if (![0, 1, 2].includes(c.state)) throw new Error("Unsupported deployment control state");
  if (BigInt(c.expires_ms) !== BigInt(0) && BigInt(c.expires_ms) <= BigInt(c.not_before_ms)) throw new Error("Expiry must follow not-before");
  if ((BigInt(c.window_start_ms) !== BigInt(0) || BigInt(c.window_end_ms) !== BigInt(0)) && BigInt(c.window_end_ms) <= BigInt(c.window_start_ms)) throw new Error("Maintenance window end must follow start");
  if (BigInt(c.notice_ms) > BigInt(604800000)) throw new Error("Notice must be at most seven days");
}
export function buildDeploymentControlTx(id: string, c: DeploymentControl, fleet?: NamedFleet): Transaction {
  requireManifest(); validateControl(c);
  const tx = new Transaction();
  if (fleet) {
    tx.moveCall({ target: `${PACKAGE_ID}::customer::set_control`, arguments: [tx.object(fleet.customerId), tx.object(fleet.id), tx.object(id), tx.pure.u8(c.state), tx.pure.u64(c.not_before_ms), tx.pure.u64(c.expires_ms), tx.pure.u64(c.window_start_ms), tx.pure.u64(c.window_end_ms), tx.pure.u64(c.notice_ms)] });
    return tx;
  }
  calls.deployment_manager_set_control(tx, PACKAGE_ID, id, c.state, c.not_before_ms,
    c.expires_ms, c.window_start_ms, c.window_end_ms, c.notice_ms);
  return tx;
}
export async function readDeploymentControl(client: Pick<SuiGrpcClient, "getDynamicField">, id: string): Promise<DeploymentControl> {
  let field;
  try {
    field = await client.getDynamicField({ parentId: id,
      name: { type: moveType("deployment_manager::ControlKey"), bcs: bcs.bool().serialize(false).toBytes() } });
  } catch (error) {
    if (error instanceof ObjectError && error.reason === "notFound") return { ...immediateControl };
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(moveType("deployment_manager::Control"))) throw new Error("Unexpected deployment Control type");
  const c = deployment_manager_ControlBcs.parse(field.dynamicField.value.bcs);
  validateControl(c);
  return c;
}
/** Recurring windows need the package upgrade that introduced their types. */
export function recurringSupported(): boolean {
  const m = requireManifest();
  return RECURRING_WINDOW_TYPES.every(name => name in m.type_origins);
}
/** Set (or, with null, clear) a deployment's recurring weekly window. Either
 * change bumps the Control revision, restarting device advance notice. */
export function buildRecurringWindowTx(id: string, r: Pick<RecurringWindow, "timezone" | "slots"> | null, fleet?: NamedFleet): Transaction {
  requireManifest();
  if (!recurringSupported()) throw new Error("Recurring windows require an upgraded package and regenerated manifest");
  const tx = new Transaction();
  if (!r) {
    if (fleet) calls.customer_clear_recurring_window(tx, PACKAGE_ID, fleet.customerId, fleet.id, id);
    else calls.deployment_manager_clear_recurring_window(tx, PACKAGE_ID, id);
    return tx;
  }
  validateRecurring(r);
  const days = r.slots.map(s => s.days), starts = r.slots.map(s => s.start_minute), durations = r.slots.map(s => s.duration_minutes);
  if (fleet) calls.customer_set_recurring_window(tx, PACKAGE_ID, fleet.customerId, fleet.id, id, r.timezone, days, starts, durations);
  else calls.deployment_manager_set_recurring_window(tx, PACKAGE_ID, id, r.timezone, days, starts, durations);
  return tx;
}
export async function readRecurringWindow(client: Pick<SuiGrpcClient, "getDynamicField">, id: string): Promise<RecurringWindow | null> {
  if (!recurringSupported()) return null;
  let field;
  try {
    field = await client.getDynamicField({ parentId: id,
      name: { type: moveType("deployment_manager::RecurringWindowKey"), bcs: bcs.bool().serialize(false).toBytes() } });
  } catch (error) {
    if (error instanceof ObjectError && error.reason === "notFound") return null;
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(moveType("deployment_manager::RecurringWindow"))) throw new Error("Unexpected recurring window type");
  return deployment_manager_RecurringWindowBcs.parse(field.dynamicField.value.bcs);
}
export function controlReason(c: DeploymentControl, now = Date.now(), recurring?: RecurringWindow | null): string {
  if (c.state === 2) return "Cancelled permanently; admitted applies may finish";
  if (BigInt(c.expires_ms) !== BigInt(0) && BigInt(now) >= BigInt(c.expires_ms)) return "Expired; create a new deployment for devices that missed it";
  if (c.state === 1) return "Paused; resume to admit waiting devices";
  if (BigInt(now) < BigInt(c.not_before_ms)) return "Scheduled; waiting for not-before time";
  if (BigInt(c.window_end_ms) !== BigInt(0) && (BigInt(now) < BigInt(c.window_start_ms) || BigInt(now) >= BigInt(c.window_end_ms))) return "Outside maintenance window; edit window to admit devices";
  if (recurring) {
    try { validateRecurring(recurring); } catch { return `Recurring window uses an unsupported timezone (${recurring.timezone}); devices defer`; }
    if (!recurringActive(recurring, now)) return "Outside recurring maintenance window; devices wait for the next opening";
  }
  return BigInt(c.notice_ms) > BigInt(0) ? "Active; devices wait for their durable advance notice" : "Active; eligible for admission";
}
