import { ObjectError } from '@mysten/sui/client';
import { bcs } from '@mysten/sui/bcs';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { Transaction } from '@mysten/sui/transactions';
import { normalizeStructTag } from '@mysten/sui/utils';
import { moveType, requireManifest } from './deployment-manifest';
import { deployment_manager_RolloutBcs, calls } from './fleet-protocol';
import type { NamedFleet } from './customer-fleets';

/** Bound audit deletion work as well as receipt deletion work. */
export async function decisionCleanupStep(client: Pick<SuiGrpcClient, 'getDynamicField'>, deployment: string, fleet?: NamedFleet) {
  const m = requireManifest();
  if (!m.type_origins['deployment_manager::DecisionCleanupKey']) return;
  let remaining: bigint;
  try {
    const { dynamicField } = await client.getDynamicField({ parentId: deployment,
      name: { type: moveType('deployment_manager::DecisionCleanupKey'), bcs: new Uint8Array([0]) } });
    if (dynamicField.value.type !== 'u64') throw Error('Unexpected audit cleanup cursor');
    remaining = BigInt(bcs.u64().parse(dynamicField.value.bcs));
  } catch (error) {
    if (!(error instanceof ObjectError) || error.reason !== 'notFound') throw error;
    const { dynamicField } = await client.getDynamicField({ parentId: deployment,
      name: { type: moveType('deployment_manager::RolloutKey'), bcs: new Uint8Array([0]) } });
    if (normalizeStructTag(dynamicField.value.type) !== normalizeStructTag(moveType('deployment_manager::Rollout'))) throw Error('Unexpected rollout type');
    remaining = BigInt(deployment_manager_RolloutBcs.parse(dynamicField.value.bcs).revision);
  }
  if (remaining <= BigInt(64)) return;
  const tx = new Transaction();
  if (fleet) calls.customer_clear_decision_audit(tx, m.call_package_id, fleet.customerId, fleet.id, deployment, 64);
  else calls.deployment_manager_clear_decision_audit(tx, m.call_package_id, deployment, 64);
  return tx;
}
