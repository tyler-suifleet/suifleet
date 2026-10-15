// Independent gRPC reads of the transactions that last wrote authoritative receipts.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const {pathToFileURL} = require('node:url');
const assert = require('node:assert/strict');
const req = createRequire(path.resolve('dapp/package.json'));
(async () => {
  const {SuiGrpcClient} = await import(pathToFileURL(req.resolve('@mysten/sui/grpc')));
  const [manifestFile, ...deployments] = process.argv.slice(2);
  const manifest = JSON.parse(fs.readFileSync(manifestFile));
  assert.equal(manifest.network, 'testnet');
  assert.equal(manifest.chain_identifier, '4c78adac');
  const client = new SuiGrpcClient({network:'testnet',baseUrl:manifest.rpc_url});
  const results = [];
  for (const deployment of deployments) {
    const {dynamicField} = await client.getDynamicField({parentId:deployment, name:{
      type:manifest.type_origins['deployment_manager::ReceiptsKey']+'::deployment_manager::ReceiptsKey',
      bcs:new Uint8Array([0]),
    }});
    const digest = dynamicField.previousTransaction;
    assert.ok(digest, 'receipt field has no previous transaction');
    const result = await client.getTransaction({digest,include:{effects:true}});
    assert.equal(result.$kind,'Transaction');
    const tx = result.Transaction;
    assert.equal(tx.digest,digest);
    assert.equal(tx.status.success,true);
    assert.ok(tx.effects, 'missing execution effects');
    results.push({deployment,digest,status:'success',checkpoint:tx.checkpoint,gas_used:tx.effects.gasUsed});
  }
  process.stdout.write(JSON.stringify(results)+'\n');
})().catch(error => { console.error(error.message); process.exitCode=1; });
