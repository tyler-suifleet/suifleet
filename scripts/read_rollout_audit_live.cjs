// Read-only audit acceptance. Never loads a wallet or signs a transaction.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
async function afterPruning(client, checkpoint, read, { timeoutMs = 300000, delay = ms => new Promise(resolve => setTimeout(resolve, ms)), now = Date.now } = {}) {
  const deadline = now() + timeoutMs;
  let lowest;
  do {
    const { response } = await client.ledgerService.getServiceInfo({});
    lowest = response.lowestAvailableCheckpoint;
    if (lowest !== undefined && lowest > checkpoint) return { lowest: lowest.toString(), audit: await read() };
    if (now() >= deadline) throw Error(`Retention did not advance beyond decision checkpoint ${checkpoint}; observed ${lowest ?? 'unreported'}. Acceptance remains unproven.`);
    await delay(5000);
  } while (true);
}
module.exports = { afterPruning };
if (require.main === module) (async () => {
  const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  assert.equal(input.manifest.chain_identifier, '4c78adac');
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(input.manifest);
  const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
  const client = new SuiGrpcClient({ network: 'testnet', baseUrl: input.manifest.rpc_url });
  const load = loader(); await load('lib/deployment-manifest.ts').verifyDeployment(client);
  const progress = load('lib/rollout-progress.ts');
  const currentOnly = { listDynamicFields: client.listDynamicFields.bind(client), async listEvents() { throw Error('Acceptance forbids event history reads'); } };
  const result = await afterPruning(client, BigInt(input.checkpoint), () => progress.fetchRolloutDecisions(currentOnly, input.record, BigInt(input.revision)));
  assert.equal(result.audit.complete, true); assert.equal(result.audit.decisions.length, Number(input.revision));
  assert.ok(result.audit.decisions.some(d => d.admitted.includes(input.device)), 'admitted IDs persist');
  console.log(JSON.stringify(result, (_, value) => typeof value === 'bigint' ? value.toString() : value));
})().catch(e => { console.error(e.message); process.exitCode = 1; });
