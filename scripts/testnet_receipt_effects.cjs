// Independent gRPC reads of the transactions that last wrote authoritative receipts.
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const {pathToFileURL} = require('node:url');
const assert = require('node:assert/strict');

function transportFailure(error) {
  // Connect/gRPC: deadline exceeded, resource exhausted, unavailable. Never
  // retry failed effects, missing receipts, malformed responses or assertions.
  return [4, 8, 14].includes(error.code) ||
    (error instanceof TypeError && error.message === 'fetch failed');
}
async function read(operation, {sleep = ms => new Promise(resolve => setTimeout(resolve, ms))} = {}) {
  for (let attempt = 1; ; attempt++) {
    try { return await operation(); }
    catch (error) {
      if (!transportFailure(error)) throw error;
      if (attempt === 3) throw new Error('receipt-effects: transport unavailable after 3 read attempts');
      await sleep(attempt * 1000);
    }
  }
}
async function verifyReceipts(client, manifest, deployments, options) {
  assert.equal(manifest.network, 'testnet');
  assert.equal(manifest.chain_identifier, '4c78adac');
  assert.ok(deployments.length, 'no deployments selected');
  const results = [];
  for (const deployment of deployments) {
    const {dynamicField} = await read(() => client.getDynamicField({parentId:deployment, name:{
      type:manifest.type_origins['deployment_manager::ReceiptsKey']+'::deployment_manager::ReceiptsKey',
      bcs:new Uint8Array([0]),
    }}), options);
    const digest = dynamicField.previousTransaction;
    assert.ok(digest, 'receipt field has no previous transaction');
    const result = await read(() => client.getTransaction({digest,include:{effects:true}}), options);
    assert.equal(result.$kind,'Transaction');
    const tx = result.Transaction;
    assert.equal(tx.digest,digest);
    assert.equal(tx.status.success,true);
    assert.ok(tx.effects, 'missing execution effects');
    results.push({deployment,digest,status:'success',checkpoint:tx.checkpoint,gas_used:tx.effects.gasUsed});
  }
  return results;
}
async function main() {
  const req = createRequire(path.resolve('dapp/package.json'));
  const {SuiGrpcClient} = await import(pathToFileURL(req.resolve('@mysten/sui/grpc')));
  const [manifestFile, ...deployments] = process.argv.slice(2);
  const manifest = JSON.parse(fs.readFileSync(manifestFile));
  const client = new SuiGrpcClient({network:'testnet',baseUrl:manifest.rpc_url});
  process.stdout.write(JSON.stringify(await verifyReceipts(client, manifest, deployments))+'\n');
}
if (require.main === module) main().catch(error => {
  // Do not copy provider response bodies/URLs or environment values to evidence.
  console.error(error.message === 'receipt-effects: transport unavailable after 3 read attempts'
    ? error.message : 'receipt-effects: verification failed');
  process.exitCode=1;
});
module.exports = {verifyReceipts};
