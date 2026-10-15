// Desired fleet state (SF-013): each group's revisioned component → deployment
// map. Devices reconcile toward the current revision of every group they are
// in; superseded deployments are never applied, new members pick the state up,
// and devices that leave stop following it.
import { calls, desired_state_DesiredStateBcs as DesiredStateBcs } from "./fleet-protocol";
import { requireManifest, moveType } from "./deployment-manifest";
import { Transaction } from "@mysten/sui/transactions";
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { bcs } from "@mysten/sui/bcs";
import { normalizeStructTag, normalizeSuiAddress } from "@mysten/sui/utils";
import { PACKAGE_ID } from "./constants";

export interface DesiredState {
  revision: number;
  /** Component name → DeploymentRecord ID. */
  components: Record<string, string>;
  updatedAt: number;
}

export interface DesiredChange {
  initiallyPaused?: boolean;
  componentName: string;
  version: string;
  recipeBlobId: string;
  artifactSha256: string;
  recipeSha256: string;
}

export interface DesiredConflict {
  deviceId: string;
  component: string;
  /** Group ID → the deployment it names. */
  choices: Record<string, string>;
}

/**
 * One transaction, one new revision per group: optionally create a verified
 * deployment of `change` targeting all `groupIds` and make it the desired
 * version of its component in each, and stop desiring the `remove`d
 * components. Every change in a group's revision commits together or not at all.
 */
export function buildDesiredRevisionTx(groupIds: string[], change?: DesiredChange, remove: string[] = []): Transaction {
  requireManifest();
  if (groupIds.length === 0) throw new Error("Desired state needs at least one group");
  if (!change && remove.length === 0) throw new Error("A revision must change something");
  const tx = new Transaction();
  const [record] = change
    ? calls.deployment_manager_new_deployment_verified(tx, PACKAGE_ID, change.componentName, change.version,
        change.recipeBlobId, change.artifactSha256, change.recipeSha256, groupIds, [], "0x6")
    : [undefined];
  for (const group of groupIds) {
    const [plan] = calls.desired_state_begin(tx, PACKAGE_ID, group);
    if (record) calls.desired_state_set_component(tx, PACKAGE_ID, plan, record);
    for (const name of remove) calls.desired_state_remove_component(tx, PACKAGE_ID, plan, name);
    calls.desired_state_commit(tx, PACKAGE_ID, plan, group, "0x6");
  }
  if (record && change?.initiallyPaused) calls.deployment_manager_set_control(tx, PACKAGE_ID, record, 1, 0, 0, 0, 0, 0);
  if (record) calls.deployment_manager_share_deployment(tx, PACKAGE_ID, record);
  return tx;
}

/** A group's current desired state; undefined until its first revision. */
export async function readDesiredState(client: Pick<SuiGrpcClient, "getDynamicField">, groupId: string): Promise<DesiredState | undefined> {
  let field;
  try {
    field = await client.getDynamicField({ parentId: groupId,
      name: { type: moveType("desired_state::DesiredKey"), bcs: bcs.bool().serialize(false).toBytes() } });
  } catch (error) {
    // Only a missing field means "no desired state"; other errors stay visible.
    if (error instanceof ObjectError && error.reason === "notFound") return undefined;
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(moveType("desired_state::DesiredState"))) {
    throw new Error("Unexpected DesiredState type");
  }
  const state = DesiredStateBcs.parse(field.dynamicField.value.bcs);
  return { revision: Number(state.revision), updatedAt: Number(state.updated_at),
    components: Object.fromEntries(state.components.contents.map((entry) => [entry.key, normalizeSuiAddress(entry.value)])) };
}

/** Desired state of each group, keyed by group ID (groups without one are omitted). */
export async function fetchDesiredStates(client: Pick<SuiGrpcClient, "getDynamicField">, groupIds: string[]): Promise<Record<string, DesiredState>> {
  const states = await Promise.all(groupIds.map(async (id) => [id, await readDesiredState(client, id)] as const));
  return Object.fromEntries(states.filter((entry): entry is readonly [string, DesiredState] => entry[1] !== undefined));
}

/**
 * Components that a device's groups disagree on. Devices leave such a
 * component as installed until the groups name the same deployment.
 */
export function desiredConflicts(groups: { objectId: string; deviceIds: string[] }[], states: Record<string, DesiredState>): DesiredConflict[] {
  const byDevice = new Map<string, Map<string, Record<string, string>>>();
  for (const group of groups) {
    const state = states[group.objectId];
    if (!state) continue;
    for (const device of group.deviceIds) {
      const components = byDevice.get(device) ?? new Map<string, Record<string, string>>();
      byDevice.set(device, components);
      for (const [component, deployment] of Object.entries(state.components)) {
        components.set(component, { ...components.get(component), [group.objectId]: deployment });
      }
    }
  }
  const conflicts: DesiredConflict[] = [];
  for (const [deviceId, components] of byDevice) {
    for (const [component, choices] of components) {
      if (new Set(Object.values(choices)).size > 1) conflicts.push({ deviceId, component, choices });
    }
  }
  return conflicts;
}
