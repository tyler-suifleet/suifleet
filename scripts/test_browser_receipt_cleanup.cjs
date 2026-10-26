// Exercise the browser's exact cleanup bridge/loop with real BCS and builders.
const assert = require('node:assert/strict');
const fs = require('node:fs');
// Guard the actual acceptance wiring: testing the planner alone missed this bug.
const runner = fs.readFileSync(require.resolve('./live_wallet_browser.cjs'), 'utf8');
assert.match(runner, /cleanupDeployment\(deployment, undefined/);
assert.match(runner, /window\.live\.deleteDeploymentStep\(d, f\)/);
assert.doesNotMatch(runner, /["']buildDeleteDeploymentTx["']/);
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
const { ObjectError } = appRequire('@mysten/sui/client');
const fixture = structuredClone(require('../manifests/fixture.localnet.json'));
for (const name of require('../interfaces/fleet-v1.json').optional_type_origins.receipt_shards) fixture.type_origins[name] = id('0x99');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const load = loader(), p = load('lib/fleet-protocol.ts'), app = load('lib/sui.ts');
const { buildCleanupStep } = load('../../scripts/browser/receipt-cleanup.ts');
const { cleanupDeployment } = require('./browser/receipt-cleanup-loop.cjs');
const missing = () => new ObjectError('missing', 'not found', { reason: 'notFound' });
const deploymentId = id('0xd');
async function scenario(sharded, schemaVersion, managed) {
  let exists = true, retiring = false, counts = Array(16).fill(65);
  const roots = Array.from({length:16}, (_, i) => id('0x' + (100+i).toString(16)));
  const fleet = managed ? { id:id('0xf'), customerId:id('0xc') } : undefined;
  const type = name => `${fixture.type_origins['deployment_manager::'+name]}::deployment_manager::${name}`;
  const client = {
    async getObject({objectId}) {
      if (!exists) throw missing();
      if (objectId === deploymentId) return {object:{type:type('DeploymentRecord'),content:p.deployment_manager_DeploymentRecordBcs.serialize({id:deploymentId,creator:id('0xa'),component_name:'test',version:'1',recipe_blob_id:'recipe',artifact_sha256:'aa'.repeat(32),created_at:0,schema_version:schemaVersion}).toBytes()}};
      const index = roots.indexOf(objectId); assert.ok(index >= 0);
      return {object:{content:p.deployment_manager_ReceiptShardBcs.serialize({id:objectId,deployment:deploymentId,index,count:counts[index],migration_cursor:0,migrated:true}).toBytes()}};
    },
    async getDynamicField() {
      if (!sharded) throw missing();
      return {dynamicField:{value:{type:type('ReceiptDirectory'),bcs:p.deployment_manager_ReceiptDirectoryBcs.serialize({roots,original_schema:schemaVersion,migrated:16,ready:true,retiring,remaining:roots.filter(r=>r!==id('0x0')).length}).toBytes()}}};
    }
  };
  const seen = [];
  const execute = tx => {
    const data = tx.getData();
    for (const command of data.commands) {
      const call = command.MoveCall, fn = call.function;
      seen.push(fn);
      if (fn === 'begin_receipt_cleanup') { assert.equal(retiring,false); retiring=true; assert.equal(call.module,managed?'customer':'deployment_manager'); }
      else if (fn === 'cleanup_receipt_chunk' || fn === 'retire_receipt_shard') {
        assert.equal(retiring,true);
        const root = data.inputs[call.arguments[1].Input].UnresolvedObject.objectId;
        const index = roots.indexOf(root); assert.ok(index >= 0);
        if (fn === 'cleanup_receipt_chunk') counts[index]=Math.max(0,counts[index]-64);
        else { assert.equal(counts[index],0);roots[index]=id('0x0'); }
      } else {
        assert.equal(fn, schemaVersion==='3'&&!managed?'delete_rollout':'delete_deployment');
        if (sharded) { assert.equal(retiring,true,'old browser skips retirement');assert.ok(roots.every(r=>r===id('0x0')),'roots must be retired'); }
        assert.equal(call.module,managed?'customer':schemaVersion==='2'?'component_catalog':'deployment_manager');
        if (managed) assert.deepEqual(data.inputs.slice(0,2).map(i=>i.UnresolvedObject.objectId),[fleet.customerId,fleet.id]);
        exists=false;
      }
    }
  };
  if (sharded) assert.throws(()=>execute(app.buildDeleteDeploymentTx(deploymentId,schemaVersion,fleet)),/old browser skips retirement/);
  seen.length=0;
  await cleanupDeployment({deploymentId,schemaVersion},fleet,async (record,context)=>{
    const tx = await buildCleanupStep(client,record,context);
    if (!tx) return {done:true};
    execute(tx);return {done:false};
  });
  assert.equal(exists,false);
  if (sharded) { assert.equal(seen[0],'begin_receipt_cleanup');assert.equal(seen.filter(s=>s==='cleanup_receipt_chunk').length,32);assert.equal(seen.filter(s=>s==='retire_receipt_shard').length,16); }
  else assert.equal(seen.length,1);
}
(async()=>{
  for (const schema of ['1','2','3']) for (const managed of [false,true]) for (const sharded of [false,true]) await scenario(sharded,schema,managed);
  await assert.rejects(buildCleanupStep({getObject:async()=>{throw Error('transport failed');}}, {deploymentId,schemaVersion:'1'}),/transport failed/);
  await assert.rejects(buildCleanupStep({getObject:async()=>{throw new ObjectError('forbidden', 'denied', {reason:'unknown'});}}, {deploymentId,schemaVersion:'1'}),/denied/);
  await assert.rejects(buildCleanupStep({getObject:async()=>({object:{}}),getDynamicField:async()=>{throw missing();}}, {deploymentId,schemaVersion:'4'}),/Unsupported DeploymentRecord schema/);
  let attempts=0;
  await assert.rejects(cleanupDeployment({},undefined,async()=>{attempts++;throw Error('failed effects');}),/failed effects/);assert.equal(attempts,1);
  await assert.rejects(cleanupDeployment({},undefined,async()=>({done:false})),/100 bounded steps/);
  console.log('PASS: browser cleanup phases, populated shards, legacy schemas 1/2/3, managed authority context, absence confirmation and error/bound refusals');
})().catch(error=>{console.error(error);process.exitCode=1;});
