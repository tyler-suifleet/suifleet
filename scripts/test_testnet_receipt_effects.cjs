// Offline failure-path tests: no chain, signing or mutations.
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const {createRequire} = require('node:module');
const {pathToFileURL} = require('node:url');
const {verifyReceipts} = require('./testnet_receipt_effects.cjs');
const manifest = {network:'testnet', chain_identifier:'4c78adac', type_origins:{'deployment_manager::ReceiptsKey':'0x1'}};
const ok = () => ({$kind:'Transaction', Transaction:{digest:'digest',status:{success:true},effects:{gasUsed:{}},checkpoint:'10'}});
function client() {
  return {getDynamicField:async () => ({dynamicField:{previousTransaction:'digest'}}), getTransaction:async () => ok()};
}
(async () => {
  // Reproduce the old one-shot failure, then recover the same read with no writes.
  for (const method of ['getDynamicField', 'getTransaction']) {
    for (const failure of [Object.assign(Error('429'),{code:'RESOURCE_EXHAUSTED'}), Object.assign(Error('unavailable'),{code:'UNAVAILABLE'}),
      Object.assign(Error('deadline'),{code:'DEADLINE_EXCEEDED'}), Object.assign(Error('fetch failed'),{code:'INTERNAL'}),
      ...[4,8,14].map(code => Object.assign(Error('transport'),{code})), new TypeError('fetch failed')]) {
      const c = client(), original = c[method]; let attempts = 0; const delays = [];
      c[method] = async args => { if (++attempts < 3) throw failure; return original(args); };
      const results = await verifyReceipts(c,manifest,['deployment'],{sleep:async ms => delays.push(ms)});
      assert.equal(attempts,3); assert.deepEqual(delays,[1000,2000]);
      assert.equal(results[0].digest,'digest'); assert.equal(results[0].status,'success');
    }
    const c = client(); let attempts = 0;
    c[method] = async () => { attempts++; throw Object.assign(Error('provider response'),{code:'UNAVAILABLE'}); };
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
  for (const code of [3,5,7,16,'INVALID_ARGUMENT','NOT_FOUND','PERMISSION_DENIED','UNAUTHENTICATED','INTERNAL','ERR_ASSERTION']) {
    const c = client(); let attempts = 0;
    c.getDynamicField = async () => { attempts++; throw Object.assign(Error('429'),{code}); };
    await assert.rejects(verifyReceipts(c,manifest,['deployment'])); assert.equal(attempts,1);
  }
  await assert.rejects(verifyReceipts(client(),{...manifest,chain_identifier:'other'},['deployment']));
  await assert.rejects(verifyReceipts(client(),manifest,[]));
  // Exercise the actual pinned SDK/transport, rather than assuming its error shape.
  const req = createRequire(path.resolve('dapp/package.json'));
  const {SuiGrpcClient} = await import(pathToFileURL(req.resolve('@mysten/sui/grpc')));
  for (const status of [429,503,504,403,404,500]) {
    let hits = 0;
    const server = http.createServer((request,response) => { hits++; response.writeHead(status); response.end(); });
    await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
    try {
      const real = new SuiGrpcClient({network:'testnet',baseUrl:`http://127.0.0.1:${server.address().port}`});
      for (const method of ['getDynamicField','getTransaction']) {
        hits = 0; const delays = [];
        const c = client();
        c[method] = () => method === 'getTransaction'
          ? real.getTransaction({digest:'11111111111111111111111111111111',include:{effects:true}})
          : real.getDynamicField({parentId:'0x1',name:{type:'0x1::test::Key',bcs:new Uint8Array([0])}});
        const retryable = [429,503,504].includes(status);
        await assert.rejects(verifyReceipts(c,manifest,['deployment'],{sleep:async ms => delays.push(ms)}),
          error => retryable ? error.message === 'receipt-effects: transport unavailable after 3 read attempts'
            : error.message !== 'receipt-effects: transport unavailable after 3 read attempts');
        assert.equal(hits,retryable ? 3 : 1,`${method} HTTP ${status}`);
        assert.deepEqual(delays,retryable ? [1000,2000] : []);
      }
    } finally { await new Promise(resolve => server.close(resolve)); }
  }
  console.log('PASS: receipt effects bounded read retries, recovery, exhaustion, fail-closed integrity/auth/not-found and chain checks (offline)');
})().catch(error => {console.error(error);process.exitCode=1;});
