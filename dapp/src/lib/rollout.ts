import { bcs } from "@mysten/sui/bcs";
import { Transaction } from "@mysten/sui/transactions";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import { calls, deployment_manager_RolloutBcs } from "./fleet-protocol";
import { PACKAGE_ID } from "./constants";
import { moveType, requireManifest } from "./deployment-manifest";
import { fetchFleetHealth, type DeviceHealth } from "./telemetry";

export type Rollout = ReturnType<typeof deployment_manager_RolloutBcs.parse>;
export interface RolloutInput {
  component: string; version: string; recipe: string; artifactSha256: string; recipeSha256: string;
  cohorts: string[][]; concurrency: number; rateCount: number; rateWindowMs: number;
  bakeMs: number; minimumSamples: number; failureBps: number; offlineTimeoutMs: number;
}
export function validateRollout(input: RolloutInput) {
  const n = input.cohorts.flat().length;
  if (!n || n > 512 || input.cohorts.some(c => c.length < input.minimumSamples)) throw new Error("Each cohort needs the minimum sample; at most 512 members total");
  const members = input.cohorts.flat().map(id => normalizeSuiAddress(id));
  if (new Set(members).size !== n) throw new Error("A device may occur in only one cohort");
  for (const [value, max, min] of [[input.concurrency, 512, 1], [input.rateCount, 512, 1],
    [input.minimumSamples, 512, 1], [input.failureBps, 10000, 1], [input.rateWindowMs, 604800000, 1],
    [input.bakeMs, 604800000, 0], [input.offlineTimeoutMs, 604800000, 1]]) {
    if (!Number.isSafeInteger(value) || value < min || value > max) throw new Error("Invalid rollout policy limit");
  }
  if (!input.component || !input.version || !input.recipe || !/^[a-f0-9]{64}$/.test(input.artifactSha256) || !/^[a-f0-9]{64}$/.test(input.recipeSha256)) throw new Error("Provide component, version, recipe blob and SHA-256 digests");
}
export function buildRolloutTx(input: RolloutInput) {
  requireManifest(); validateRollout(input);
  const tx = new Transaction();
  const [policy] = calls.deployment_manager_rollout_policy(tx, PACKAGE_ID, input.concurrency, input.rateCount,
    input.rateWindowMs, input.bakeMs, input.minimumSamples, input.failureBps, input.offlineTimeoutMs);
  const [record] = calls.deployment_manager_new_rollout_verified(tx, PACKAGE_ID, input.component, input.version,
    input.recipe, input.artifactSha256, input.recipeSha256, [], input.cohorts.flat(),
    input.cohorts.flatMap((cohort, i) => cohort.map(() => i)), policy, "0x6");
  calls.deployment_manager_share_deployment(tx, PACKAGE_ID, record);
  return tx;
}
export async function readRollout(client: Pick<SuiGrpcClient, "getDynamicField">, id: string): Promise<Rollout> {
  const { dynamicField } = await client.getDynamicField({ parentId: id,
    name: { type: moveType("deployment_manager::RolloutKey"), bcs: bcs.bool().serialize(false).toBytes() } });
  if (normalizeStructTag(dynamicField.value.type) !== normalizeStructTag(moveType("deployment_manager::Rollout"))) throw new Error("Unexpected rollout policy type");
  return deployment_manager_RolloutBcs.parse(dynamicField.value.bcs);
}
/** Fresh signed health must describe this installed deployment, after its grant.
 * Offline/unavailable/unverified reports supply no permission or success sample.
 * The creator signs this attestation; chain state is the durable coordinator.
 */
export function rolloutHealth(r: Rollout, id: string, health: Record<string, DeviceHealth>, now = Date.now()) {
  const online: string[] = [], healthy: string[] = [], unhealthy: string[] = [];
  r.members.forEach((device, i) => {
    const h = health[device];
    if (!h || !["healthy", "degraded"].includes(h.status) || h.observedMs === undefined ||
      h.observedMs > now || now - h.observedMs > 120000) return;
    online.push(device);
    const component = h.components.find(c => normalizeSuiAddress(c.deployment_id) === normalizeSuiAddress(id));
    if (!component || !component.installed || BigInt(h.observedMs) < BigInt(r.admitted_at[i])) return;
    if (h.status === "healthy") healthy.push(device);
    else unhealthy.push(device);
  });
  return { online, healthy, unhealthy };
}
export async function buildAdvanceRolloutTx(client: Pick<SuiGrpcClient, "getDynamicField">, id: string, owner: string, token: string) {
  requireManifest();
  const r = await readRollout(client, id);
  const health = await fetchFleetHealth(client, r.members, owner, undefined, token);
  const a = rolloutHealth(r, id, health);
  const tx = new Transaction();
  calls.deployment_manager_advance_rollout(tx, PACKAGE_ID, id, a.online, a.healthy, a.unhealthy, "0x6");
  return tx;
}
export function buildRolloutControlTx(id: string, state: number, skip: string[], reason: string) {
  requireManifest();
  if (!Number.isInteger(state) || state < 0 || state > 2) throw new Error("Invalid rollout control state");
  if (!reason.trim() || new TextEncoder().encode(reason).length > 256) throw new Error("Provide an audit reason (1–256 bytes)");
  const tx = new Transaction();
  calls.deployment_manager_control_rollout(tx, PACKAGE_ID, id, state, skip, reason, "0x6");
  return tx;
}
