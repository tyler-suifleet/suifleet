// Read-only audit acceptance. Never loads a wallet or signs a transaction.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
class RetentionPending extends Error {}
async function afterPruning(client, checkpoint, read, { timeoutMs = 300000, delay = ms => new Promise(resolve => setTimeout(resolve, ms)), now = Date.now } = {}) {
  const deadline = now() + timeoutMs;
  let lowest;
  do {
    const { response } = await client.ledgerService.getServiceInfo({});
    lowest = response.lowestAvailableCheckpoint;
    if (lowest !== undefined && lowest > checkpoint) return { lowest: lowest.toString(), audit: await read() };
    if (now() >= deadline) throw new RetentionPending(`Retention did not advance beyond decision checkpoint ${checkpoint}; observed ${lowest ?? 'unreported'}. Acceptance remains unproven.`);
    await delay(5000);
  } while (true);
}
async function verifyRetainedFixture(client, load, input) {
  const rollout = load('lib/rollout.ts');
  const target = await rollout.readRolloutTarget(client, input.record, input.operator);
  assert.equal(target.componentName, 'audit-only'); assert.equal(target.version, '1');
  const stored = await rollout.readRollout(client, input.record);
  assert.equal(stored.revision, String(input.revision), 'retained audit revision changed');
  assert.deepEqual(stored.members, [input.device], 'retained audit membership changed');
}
module.exports = { afterPruning, verifyRetainedFixture };
if (require.main === module) (async () => {
  const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  assert.equal(input.manifest.chain_identifier, '4c78adac');
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(input.manifest);
  const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
  const client = new SuiGrpcClient({ network: 'testnet', baseUrl: input.manifest.rpc_url });
  const load = loader(); await load('lib/deployment-manifest.ts').verifyDeployment(client);
  await verifyRetainedFixture(client, load, input);
  const progress = load('lib/rollout-progress.ts');
  const currentOnly = { listDynamicFields: client.listDynamicFields.bind(client), async listEvents() { throw Error('Acceptance forbids event history reads'); } };
  const result = await afterPruning(client, BigInt(input.checkpoint), () => progress.fetchRolloutDecisions(currentOnly, input.record, BigInt(input.revision)), { timeoutMs: input.timeoutMs ?? 300000 });
  assert.equal(result.audit.complete, true); assert.equal(result.audit.decisions.length, Number(input.revision));
  assert.ok(result.audit.decisions.some(d => d.admitted.includes(input.device)), 'admitted IDs persist');
  console.log(JSON.stringify(result, (_, value) => typeof value === 'bigint' ? value.toString() : value));
})().catch(e => { console.error(e.message); process.exitCode = e instanceof RetentionPending ? 3 : 1; });
