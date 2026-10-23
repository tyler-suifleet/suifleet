const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const manifest = require('../manifests/fixture.localnet.json');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const { toBase64 } = appRequire('@mysten/sui/utils');
const bytes = s => new TextEncoder().encode(s);
const store = new Map();
global.window = { localStorage: { getItem: k => store.get(k) ?? null, setItem: (k,v) => store.set(k,v) } };
function fixture() {
  let lowest = 0n, calls = [], frames = [], fail = false;
  const client = { ledgerService: {
    getServiceInfo: async () => ({ response: { lowestAvailableCheckpoint: lowest } }),
    listEvents: request => { calls.push(request); return { responses: (async function* () {
      if (fail) throw Error('transport unavailable');
      yield* frames;
    })() }; },
  } };
  return { client, calls, floor: n => lowest = n, fail: b => fail = b, frames: f => frames = f };
}
const end = (cursor, checkpoint, reason = 5) => ({ watermark: { cursor: bytes(cursor), checkpoint }, end: { reason } });
const event = n => ({ event: { transactionDigest: `digest${n}`, eventIndex: 0, checkpoint: BigInt(n), contents: { value: bytes(`event${n}`) },
  packageId: manifest.call_package_id, module: 'test', sender: '0xa', eventType: '0x1::test::Created' }, watermark: { cursor: bytes(`event${n}`), checkpoint: BigInt(n-1) } });
(async () => {
  let history = loader()('lib/event-history.ts');
  const f = fixture(); f.frames([event(1), end('tip1', 1n)]);
  let result = await history.incrementalEvents(f.client, '0x1::test::Created', '0xa');
  assert.equal(result.length, 1);
  const literals = f.calls[0].filter.terms[0].literals;
  assert.equal(literals[0].predicate.eventType.eventType, '0x1::test::Created');
  assert.equal(literals[1].predicate.sender.address.slice(-1), 'a');
  // Reload module/client: cursor and BCS both survive browser persistence.
  history = loader()('lib/event-history.ts');
  const reloaded = fixture(); reloaded.frames([event(2), end('tip2', 2n)]);
  result = await history.incrementalEvents(reloaded.client, '0x1::test::Created', '0xa');
  assert.equal(new TextDecoder().decode(reloaded.calls[0].options.after), 'tip1');
  assert.deepEqual(result.map(e => new TextDecoder().decode(e.bcs)), ['event2','event1']);
  // Empty pages advance the persisted frontier and do not rescan genesis.
  reloaded.frames([end('tip3', 3n)]);
  await Promise.all([history.incrementalEvents(reloaded.client, '0x1::test::Created', '0xa'), history.incrementalEvents(reloaded.client, '0x1::test::Created', '0xa')]);
  assert.equal(reloaded.calls.length, 2, 'concurrent scan coalesced');
  reloaded.frames([end('tip4', 4n)]);
  await history.incrementalEvents(reloaded.client, '0x1::test::Created', '0xa');
  assert.equal(toBase64(reloaded.calls[2].options.after), toBase64(bytes('tip3')));
  reloaded.floor(6n);
  await assert.rejects(history.incrementalEvents(reloaded.client, '0x1::test::Created', '0xa'), /History unavailable/);
  assert.equal(reloaded.calls.length, 3, 'pruned cursor rejected before scan');
  // First scan on a pruned endpoint reads the available range with an explicit warning.
  const partial = fixture(); partial.floor(50n); partial.frames([end('partial', 51n)]);
  assert.deepEqual(await history.incrementalEvents(partial.client, '0x1::test::Partial'), []);
  assert.match(history.historySnapshot(), /History unavailable before checkpoint 50/);
  assert.equal(partial.calls[0].startCheckpoint, 50n);
  const broken = fixture(); broken.frames([end('stuck', 0n, 2)]);
  await assert.rejects(history.incrementalEvents(broken.client, '0x1::test::Broken'), /no progress/);
  const noEnd = fixture(); noEnd.frames([event(1)]);
  await assert.rejects(history.incrementalEvents(noEnd.client, '0x1::test::Interrupted'), /terminal frame/);
  const failing = fixture(); failing.fail(true);
  await assert.rejects(history.incrementalEvents(failing.client, '0x1::test::Failure'), /transport unavailable/);
  assert.equal([...store.keys()].some(k => k.endsWith('Failure:*')), false, 'no cursor committed on failure');
  const quota = fixture(); quota.frames([end('quota', 1n)]);
  window.localStorage.setItem = () => { throw Error('quota exceeded'); };
  await assert.rejects(history.incrementalEvents(quota.client, '0x1::test::Quota'), /quota exceeded/);
  console.log('PASS: type/sender filters; persisted cursor+BCS; reload, empty scan progress, concurrency, pruned ranges, incomplete/failed streams and storage failures');
})().catch(e => { console.error(e); process.exitCode = 1; });
