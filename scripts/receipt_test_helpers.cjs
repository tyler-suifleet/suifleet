const { loader } = require('./frontend_test_utils.cjs');
async function deleteDeployment(client, execute, id, schema = '1') {
  const { buildCleanupStep } = loader()('../../scripts/browser/receipt-cleanup.ts');
  await require('./browser/receipt-cleanup-loop.cjs').cleanupDeployment({ deploymentId: id, schemaVersion: schema }, undefined, async (deployment, fleet) => {
    const tx = await buildCleanupStep(client, deployment, fleet);
    if (!tx) return { done: true };
    await execute(tx);
    return { done: false };
  });
}
async function report(client, tx, packageId, deployment, registry, cap, device, status) {
  const load = loader(), p = load('lib/fleet-protocol.ts'), shards = load('lib/receipt-shards.ts');
  const directory = await shards.readReceiptDirectory(client, deployment);
  if (directory) p.calls.deployment_manager_report_status_v3(tx, packageId, deployment, directory.roots[shards.receiptShardIndex(device)], registry, cap, status);
  else p.calls.deployment_manager_report_verified_status(tx, packageId, deployment, registry, cap, status);
}
module.exports = { deleteDeployment, report };
