// SF-084 Rollouts view over real chain data: executed by the isolated
// frontend/gRPC acceptance harness. The dApp's own builders create, pause,
// skip, admit and (through a real device failure report) abort a canary
// rollout; the dashboard's readers must then show exactly the on-chain stages,
// grants, threshold and abort state and the audited decision history.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { Transaction } = appRequire('@mysten/sui/transactions');
const { normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
module.exports = async ({ client, execute, operator, deviceSigner, device, cap, registryId, packageId }) => {
  const load = loader(), sui = load('lib/sui.ts'), rollout = load('lib/rollout.ts'), progress = load('lib/rollout-progress.ts'), protocol = load('lib/fleet-protocol.ts');
  const owner = operator.toSuiAddress();
  const members = [device];
  for (const name of ['canary-b', 'canary-c']) {
    await execute(sui.buildRegisterDeviceTx(name, Ed25519Keypair.generate().toSuiAddress(), 'aarch64', owner));
    members.push((await sui.fetchDevices(client, owner)).find(d => d.name === name).objectId);
  }
  const created = await execute(rollout.buildRolloutTx({ component: 'canary-app', version: '1.0.0', recipe: 'canary-recipe',
    artifactSha256: 'ab'.repeat(32), recipeSha256: 'cd'.repeat(32), cohorts: [[members[0]], [members[1], members[2]]],
    concurrency: 1, rateCount: 1, rateWindowMs: 1000, bakeMs: 0, minimumSamples: 1, failureBps: 5000, offlineTimeoutMs: 604800000 }));
  const record = Object.keys(created.objectTypes).find(o => created.objectTypes[o].endsWith('::deployment_manager::DeploymentRecord'));
  assert.ok(record);
  const view = async () => progress.rolloutProgress(await rollout.readRollout(client, record), Date.now());
  let p = await view();
  assert.equal(p.state, 'Active'); assert.equal(p.revision, 1n); assert.equal(p.currentCohort, 0);
  assert.deepEqual(p.stages.map(s => [s.status, s.members, s.waiting]), [['current', 1, 1], ['pending', 2, 2]]);
  assert.deepEqual([p.policy.failureBps, p.policy.minimumSamples], [5000n, 1n]);
  await execute(rollout.buildRolloutControlTx(record, 1, [], 'hold for review'));
  assert.equal((await view()).state, 'Paused');
  await execute(rollout.buildRolloutControlTx(record, 0, [members[2]], 'retire canary-c'));
  p = await view();
  assert.equal(p.state, 'Active'); assert.equal(p.stages[1].skipped, 1);
  const advance = (online) => {
    const tx = new Transaction();
    protocol.calls.deployment_manager_advance_rollout_onchain(tx, packageId, record, registryId, online, '0x6'); return tx; };
  await execute(advance([members[0]]));
  p = await view();
  assert.deepEqual([p.stages[0].granted, p.stages[0].inflight, p.members[0].status], [1, 1, 'granted']);
  assert.ok(p.members[0].grantedAtMs > 0n);
  const report = new Transaction();
  await require("./receipt_test_helpers.cjs").report(client, report, packageId, record, registryId, cap, device, protocol.STATUS.failed);
  await execute(report, deviceSigner);
  p = await view();
  assert.deepEqual([p.samples, p.failures], [0, 0], 'v3 report did not mutate the coordinator');
  const settle = new Transaction();
  await rollout.appendRolloutReceiptSync(client, settle, record, members);
  await execute(settle);
  p = await view();
  assert.deepEqual([p.samples, p.failures, p.thresholdReached, p.state], [1, 1, true, 'Active']);
  await execute(advance([]));
  p = await view();
  assert.deepEqual([p.state, p.aborted, p.revision, p.stages[0].failed, p.stages[1].status, p.stages[1].waiting], ['Aborted', true, 5n, 1, 'pending', 1]);
  const { decisions, complete } = await progress.fetchRolloutDecisions(client, record, p.revision);
  assert.equal(complete, true);
  assert.deepEqual(decisions.map(d => [d.revision, d.state, d.reason]), [
    [5n, 'Aborted', 'failure threshold reached; permanently aborted'], [4n, 'Active', 'admitted cohort members'],
    [3n, 'Active', 'retire canary-c'], [2n, 'Paused', 'hold for review'], [1n, 'Active', 'created; no members admitted']]);
  assert.deepEqual(decisions[1].admitted, [id(members[0])]);
  // An aborted rollout cannot be resumed through the dApp's control builder.
  await assert.rejects(execute(rollout.buildRolloutControlTx(record, 0, [], 'resume after abort')));
  assert.equal((await view()).state, 'Aborted');
  await require("./receipt_test_helpers.cjs").deleteDeployment(client, execute, record, "3");
  for (const objectId of members.slice(1)) await execute(sui.buildDeregisterDeviceTx((await sui.fetchDevices(client, owner)).find(d => d.objectId === objectId)));
  console.log(`PASS: real gRPC SF-084 rollout view: dApp builders paused/skipped/admitted, device failure report aborted at the threshold; readers show stages, grants, threshold, abort and all 5 audited decisions (${record})`);
};
