const fs = require('node:fs'), assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const m = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), r = load('lib/rollout.ts'), p = load('lib/fleet-protocol.ts');
const input = {component:'app',version:'1',recipe:'blob',artifactSha256:'a'.repeat(64),recipeSha256:'b'.repeat(64),cohorts:[['0x1'],['0x2']],concurrency:1,rateCount:1,rateWindowMs:1000,bakeMs:100,minimumSamples:1,failureBps:5000,offlineTimeoutMs:2000};
(async () => {
  assert.deepEqual(r.buildRolloutTx(input).getData().commands.map(c=>c.MoveCall.function), ['rollout_policy','new_rollout_verified','share_deployment']);
  for (const bad of [{concurrency:0},{rateCount:513},{bakeMs:-1},{minimumSamples:2},{failureBps:0},{cohorts:[['0x1'],['0x01']]},{artifactSha256:'bad'}]) assert.throws(()=>r.buildRolloutTx({...input,...bad}), JSON.stringify(bad));
  assert.throws(()=>r.buildRolloutControlTx('0x42',1,[],''));
  const {normalizeSuiAddress:n} = appRequire('@mysten/sui/utils');
  const state = {policy:{concurrency:'1',rate_count:'1',rate_window_ms:'1000',bake_ms:'100',minimum_samples:'1',failure_bps:'5000',offline_timeout_ms:'2000'},members:[n('0x1'),n('0x2')],cohorts:['0','1'],admitted_at:['100','0'],outcomes:[1,0],skipped:[false,false],cohort:'0',state:0,bake_started_ms:'0',progress_ms:'100',revision:'1'};
  const health = {[n('0x1')]:{status:'healthy',observedMs:150,components:[{deployment_id:'0x42',installed:true}]}};
  assert.deepEqual(r.rolloutHealth(state,'0x42',health,200),{online:[n('0x1')],healthy:[n('0x1')],unhealthy:[]});
  for (const change of [{status:'unverified'},{status:'offline'},{observedMs:99},{observedMs:201},{observedMs:0}]) {
    assert.equal(r.rolloutHealth(state,'0x42',{[n('0x1')]:{...health[n('0x1')],...change}},200).healthy.length,0);
  }
  assert.equal(r.rolloutHealth(state,'0x43',health,200).healthy.length,0);
  let key;
  const result = {dynamicField:{value:{type:`${m.type_origins['deployment_manager::Rollout']}::deployment_manager::Rollout`,bcs:p.deployment_manager_RolloutBcs.serialize(state).toBytes()}}};
  assert.deepEqual(await r.readRollout({getDynamicField:async args=>{key=args.name.type;return result;}},'0x42'),state);
  assert.equal(key,`${m.type_origins['deployment_manager::RolloutKey']}::deployment_manager::RolloutKey`);
  await assert.rejects(r.readRollout({getDynamicField:async()=>{throw Error('offline');}},'0x42'),/offline/);
  console.log('PASS: rollout encoders, bounds, duplicate cohorts, mixed-origin BCS, health freshness/deployment binding and fail-closed reads');
})().catch(e=>{console.error(e);process.exitCode=1;});
