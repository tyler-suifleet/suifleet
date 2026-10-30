// Offline failure-path tests: no chain, signing or mutations.
const assert = require('node:assert/strict');
const {verifyReceipts} = require('./testnet_receipt_effects.cjs');
const manifest = {network:'testnet', chain_identifier:'4c78adac', type_origins:{'deployment_manager::ReceiptsKey':'0x1'}};
const ok = () => ({$kind:'Transaction', Transaction:{digest:'digest',status:{success:true},effects:{gasUsed:{}},checkpoint:'10'}});
function client() {
  return {getDynamicField:async () => ({dynamicField:{previousTransaction:'digest'}}), getTransaction:async () => ok()};
}
(async () => {
  // Reproduce the old one-shot failure, then recover the same read with no writes.
  for (const method of ['getDynamicField', 'getTransaction']) {
    for (const failure of [Object.assign(Error('429'),{code:8}), Object.assign(Error('unavailable'),{code:14}),
      Object.assign(Error('deadline'),{code:4}), new TypeError('fetch failed')]) {
      const c = client(), original = c[method]; let attempts = 0; const delays = [];
      c[method] = async args => { if (++attempts < 3) throw failure; return original(args); };
      const results = await verifyReceipts(c,manifest,['deployment'],{sleep:async ms => delays.push(ms)});
      assert.equal(attempts,3); assert.deepEqual(delays,[1000,2000]);
      assert.equal(results[0].digest,'digest'); assert.equal(results[0].status,'success');
    }
    const c = client(); let attempts = 0;
    c[method] = async () => { attempts++; throw Object.assign(Error('provider response'),{code:14}); };
    await assert.rejects(verifyReceipts(c,manifest,['deployment'],{sleep:async () => {}}),
      /^Error: receipt-effects: transport unavailable after 3 read attempts$/);
    assert.equal(attempts,3);
  }
  for (const response of [{$kind:'FailedTransaction'}, {$kind:'Transaction',Transaction:{...ok().Transaction,digest:'other'}},
    {$kind:'Transaction',Transaction:{...ok().Transaction,status:{success:false}}},
    {$kind:'Transaction',Transaction:{...ok().Transaction,effects:undefined}}]) {
    const c = client(); let attempts = 0;
    c.getTransaction = async () => { attempts++; return response; };
    await assert.rejects(verifyReceipts(c,manifest,['deployment'],{sleep:async () => assert.fail('assertion retried')}));
    assert.equal(attempts,1);
  }
  const c = client(); let attempts = 0;
  c.getDynamicField = async () => { attempts++; return {dynamicField:{}}; };
  await assert.rejects(verifyReceipts(c,manifest,['deployment']),/no previous transaction/); assert.equal(attempts,1);
  for (const code of [3,5,7,16,'ERR_ASSERTION']) {
    const c = client(); let attempts = 0;
    c.getDynamicField = async () => { attempts++; throw Object.assign(Error('429'),{code}); };
    await assert.rejects(verifyReceipts(c,manifest,['deployment'])); assert.equal(attempts,1);
  }
  await assert.rejects(verifyReceipts(client(),{...manifest,chain_identifier:'other'},['deployment']));
  await assert.rejects(verifyReceipts(client(),manifest,[]));
  console.log('PASS: receipt effects bounded read retries, recovery, exhaustion, fail-closed integrity/auth/not-found and chain checks (offline)');
})().catch(error => {console.error(error);process.exitCode=1;});
