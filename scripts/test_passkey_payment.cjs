// Exercise the real pinned SDK serializer without network or simulated success.
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const req = createRequire(require('node:path').resolve('dapp/package.json'));
const { Transaction } = req('@mysten/sui/transactions');
const { resolvePayment } = require('./passkey_transaction.cjs');
const owner = '0x' + '1'.repeat(64), sponsor = '0x' + '2'.repeat(64);
const digest = '11111111111111111111111111111111';
(async () => {
 for (const addressBalance of ['30000000', '0']) {
  for (const payer of [owner, sponsor]) {
   let simulations = 0;
   const client = { core: {
    getReferenceGasPrice: async () => ({referenceGasPrice:'1000'}),
    getBalance: async ({owner: actual}) => { assert.equal(actual, payer);return {balance:{addressBalance}}; },
    listCoins: async ({owner: actual}) => { assert.equal(actual, payer);return {objects: addressBalance === '0' ? [{objectId:'0x3',version:'1',digest,balance:'30000000'}] : []}; },
    getCurrentSystemState: async () => ({systemState:{referenceGasPrice:'1000',epoch:'1'}}),
    getChainIdentifier: async () => ({chainIdentifier:'69WiPg3DAQiwdxfncX6wYQ2siKwAe6L9BZthQea3JNMD'}),
    simulateTransaction: async () => { simulations++;throw Error('refusals must reach validators'); },
   }};
   const tx = new Transaction();
   tx.moveCall({target:'0x4::customer::set_security',arguments:[tx.pure.vector('u8',Array(7).fill(2))]});
   const bytes = await resolvePayment(tx, client, payer, owner, 20000000);
   const data = Transaction.from(bytes).getData();
   assert.equal(data.sender,owner);assert.equal(data.gasData.owner,payer);
   assert.equal(data.gasData.budget,'20000000');assert.equal(simulations,0);
   if (addressBalance !== '0') {
    assert.deepEqual(data.gasData.payment,[]);
    assert.equal(data.expiration.$kind,'ValidDuring');
    assert.equal(data.expiration.ValidDuring.chain,'69WiPg3DAQiwdxfncX6wYQ2siKwAe6L9BZthQea3JNMD');
   } else { assert.equal(data.gasData.payment.length,1); }
  }
 }
 // Coin pagination/fragmentation must not reject a funded wallet either.
 const coins = [{objectId:'0x3',version:'1',digest,balance:'10000000'},
                {objectId:'0x4',version:'1',digest,balance:'10000000'}];
 const core = {
  getBalance:async()=>({balance:{addressBalance:'0'}}),
  getReferenceGasPrice:async()=>({referenceGasPrice:'1000'}),
  listCoins:async({cursor})=>({objects:[coins[cursor ? 1 : 0]],cursor:cursor ? null : 'page2',hasNextPage:!cursor}),
 };
 const tx = new Transaction();
 const bytes = await resolvePayment(tx,{core},owner,owner,20000000);
 assert.equal(Transaction.from(bytes).getData().gasData.payment.length,2);
 core.listCoins=async()=>({objects:[],hasNextPage:false});
 await assert.rejects(resolvePayment(new Transaction(),{core},owner,owner,20000000),/fixture gas coin unavailable/);
 core.listCoins=async()=>({objects:[],hasNextPage:true,cursor:null});
 await assert.rejects(resolvePayment(new Transaction(),{core},owner,owner,20000000),/invalid fixture gas pagination/);
 console.log('PASS native wallet/sponsor coin and coinless address-balance gas resolution; fixed budget and no simulation');
})().catch(error => { console.error(error);process.exitCode=1; });
