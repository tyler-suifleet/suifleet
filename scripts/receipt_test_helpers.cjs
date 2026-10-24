const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { ObjectError } = appRequire('@mysten/sui/client');
async function deleteDeployment(client, execute, id, schema = '1') {
  const sui = loader()('lib/sui.ts');
  for (let step = 0; step < 100; step++) {
    try { await client.getObject({ objectId: id }); }
    catch (error) { if (error instanceof ObjectError && error.reason === 'notFound') return; throw error; }
    await execute(await sui.buildDeleteDeploymentStepTx(client, id, schema));
  }
  throw Error('Deletion did not finish in 100 bounded steps');
}
async function report(client, tx, packageId, deployment, registry, cap, device, status) {
  const load = loader(), p = load('lib/fleet-protocol.ts'), shards = load('lib/receipt-shards.ts');
  const directory = await shards.readReceiptDirectory(client, deployment);
  if (directory) p.calls.deployment_manager_report_status_v3(tx, packageId, deployment, directory.roots[shards.receiptShardIndex(device)], registry, cap, status);
  else p.calls.deployment_manager_report_verified_status(tx, packageId, deployment, registry, cap, status);
}
module.exports = { deleteDeployment, report };
