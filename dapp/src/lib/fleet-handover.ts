import { Transaction } from "@mysten/sui/transactions";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { normalizeStructTag } from "@mysten/sui/utils";
import { calls, fleet_handover_HandoverBcs } from "./fleet-protocol";
import { requireManifest, verifyDeployment, moveType, normalizeId } from "./deployment-manifest";

export type HandoverItem = { objectId: string; kind: number };
function manifest() {
  const m = requireManifest();
  if (!m.type_origins["fleet_handover::Handover"]) throw new Error("This deployment does not support fleet handover");
  return m;
}

export function buildProposeHandoverTx(recipient: string, items: HandoverItem[], expiresMs: bigint): Transaction {
  const m = manifest();
  recipient = normalizeId(recipient);
  if (BigInt(recipient) === BigInt(0)) throw new Error("Recipient must be a wallet or multisig address");
  if (!items.length || items.length > 512) throw new Error("Choose 1–512 authority records per handover");
  const ids = items.map(item => normalizeId(item.objectId));
  if (new Set(ids).size !== ids.length || items.some(item => !Number.isInteger(item.kind) || item.kind < 0 || item.kind > 4)) {
    throw new Error("Inventory has duplicate IDs or unsupported authority kinds");
  }
  if (expiresMs <= BigInt(Date.now())) throw new Error("Handover expiry must be in the future");
  const tx = new Transaction();
  calls.fleet_handover_propose(tx, m.call_package_id, recipient, ids, items.map(item => item.kind), expiresMs, "0x6");
  return tx;
}

export async function readHandover(client: SuiGrpcClient, id: string) {
  const m = manifest();
  await verifyDeployment(client, m);
  const { object } = await client.getObject({ objectId: normalizeId(id), include: { content: true } });
  if (normalizeStructTag(object.type) !== normalizeStructTag(moveType("fleet_handover::Handover")) || object.owner.$kind !== "Shared") {
    throw new Error("Unexpected handover type or ownership");
  }
  return fleet_handover_HandoverBcs.parse(object.content);
}

export function buildHandoverActionTx(id: string, action: "confirm" | "cancel" | "finish"): Transaction {
  const m = manifest();
  const tx = new Transaction();
  if (action === "confirm") calls.fleet_handover_confirm(tx, m.call_package_id, normalizeId(id), "0x6");
  else if (action === "cancel") calls.fleet_handover_cancel(tx, m.call_package_id, normalizeId(id));
  else calls.fleet_handover_finish(tx, m.call_package_id, normalizeId(id));
  return tx;
}

/** Atomic batch. A failed step rolls back every step in this transaction. */
export function buildHandoverStepsTx(id: string, items: HandoverItem[]): Transaction {
  const m = manifest();
  const tx = new Transaction();
  for (const item of items) {
    const object = normalizeId(item.objectId);
    switch (item.kind) {
      case 0: calls.fleet_handover_device(tx, m.call_package_id, normalizeId(id), object, "0x6"); break;
      case 1: calls.fleet_handover_group(tx, m.call_package_id, normalizeId(id), object, "0x6"); break;
      case 2: calls.fleet_handover_deployment(tx, m.call_package_id, normalizeId(id), object, "0x6"); break;
      case 3: calls.fleet_handover_revoke_role(tx, m.call_package_id, normalizeId(id), m.shared_objects.registry.id, object, "0x6"); break;
      case 4: calls.fleet_handover_close_enrollment(tx, m.call_package_id, normalizeId(id), object, "0x6"); break;
      default: throw new Error("Unsupported handover authority kind");
    }
  }
  if (!items.length) throw new Error("No pending authority records selected");
  return tx;
}
