import { ObjectError } from '@mysten/sui/client';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import type { NamedFleet } from '../../dapp/src/lib/customer-fleets';
import { buildDeleteDeploymentStepTx } from '../../dapp/src/lib/sui';

/** Use the application's bounded planner and confirm absence before completion. */
export async function buildCleanupStep(client: Pick<SuiGrpcClient, 'getObject' | 'getDynamicField'>,
  deployment: { deploymentId: string; schemaVersion: string }, fleet?: NamedFleet) {
  try { await client.getObject({ objectId: deployment.deploymentId }); }
  catch (error) {
    if (error instanceof ObjectError && error.reason === 'notFound') return undefined;
    throw error;
  }
  return buildDeleteDeploymentStepTx(client, deployment.deploymentId, deployment.schemaVersion, fleet);
}
