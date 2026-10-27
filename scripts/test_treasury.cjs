const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { appRequire } = require('./frontend_test_utils.cjs');
const { publicKeyFromSuiBytes } = appRequire('@mysten/sui/verify');
const { MultiSigPublicKey } = appRequire('@mysten/sui/multisig');
const { Transaction } = appRequire('@mysten/sui/transactions');
const { verifyTransactionSignature } = appRequire('@mysten/sui/verify');
(async () => {
const descriptor = JSON.parse(fs.readFileSync(path.join(__dirname,'../deployments/testnet/treasury.json'),'utf8'));
assert.equal(descriptor.network,'testnet');
assert.equal(descriptor.chain_identifier,'4c78adac');
assert.equal(descriptor.threshold,2);
assert.equal(descriptor.public_keys.length,3);
assert.equal(new Set(descriptor.public_keys.map(k => k.public_key)).size,3);
const multi = MultiSigPublicKey.fromPublicKeys({threshold:descriptor.threshold,publicKeys:descriptor.public_keys.map(k => {
  assert.equal(k.scheme,'ED25519'); assert.equal(k.weight,1);
  return {publicKey:publicKeyFromSuiBytes(k.public_key),weight:k.weight};
})});
assert.equal(multi.toSuiAddress(),descriptor.treasury_payee);
const dir = fs.mkdtempSync(path.join(os.tmpdir(),'sf185-offline-'));
try {
  const state = path.join(dir,'keys.json');
  const call = mode => execFileSync(process.execPath,[path.join(__dirname,'testnet_treasury.cjs'),mode,state],{encoding:'utf8'});
  const first = JSON.parse(call('create'));
  assert.deepEqual(JSON.parse(call('create')),first,'fixture must reuse its keys');
  assert.equal(fs.statSync(state).mode & 0o077,0);
  assert.match(call('self-test'),/PASS/);
  const tx = new Transaction();
  tx.setSender(first.treasury_payee); tx.setGasOwner(first.treasury_payee);
  tx.setGasBudget(5_000_000); tx.setGasPrice(1000);
  tx.setGasPayment([{objectId:'0x'+'11'.repeat(32),version:'1',digest:'11111111111111111111111111111111'}]);
  const [coin] = tx.splitCoins(tx.gas,[1]);
  tx.transferObjects([coin],'0x'+'22'.repeat(32));
  const bytes = await tx.build();
  const signature = execFileSync(process.execPath,[path.join(__dirname,'testnet_treasury.cjs'),'sign',state],
    {input:Buffer.from(bytes).toString('base64'),encoding:'utf8'}).trim();
  await verifyTransactionSignature(bytes,signature,{address:first.treasury_payee});
  tx.setSender('0x'+'33'.repeat(32));
  const wrongSender = await tx.build();
  assert.throws(() => execFileSync(process.execPath,[path.join(__dirname,'testnet_treasury.cjs'),'sign',state],
    {input:Buffer.from(wrongSender).toString('base64'),stdio:['pipe','pipe','pipe']}));
  console.log('PASS: pinned testnet descriptor/address; private reusable fixture; quorum and tamper rejection');
} finally {fs.rmSync(dir,{recursive:true,force:true});}

})().catch(error => {console.error(error);process.exit(1);});
