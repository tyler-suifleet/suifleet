const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const manifest = require('../manifests/fixture.localnet.json');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const { toBase64 } = appRequire('@mysten/sui/utils');
const bytes = s => new TextEncoder().encode(s);
const store = new Map();
global.window = { localStorage: { getItem: k => store.get(k) ?? null, setItem: (k,v) => store.set(k,v), removeItem: k => store.delete(k), key: i => [...store.keys()][i] ?? null, get length() { return store.size; } } };
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
  reloaded.frames([event(7), end('tip7', 7n)]);
  assert.equal((await history.incrementalEvents(reloaded.client, '0x1::test::Created', '0xa')).length, 3);
  assert.equal(reloaded.calls[3].options.after, undefined, 'pruned cursor restarts retained range while preserving known events');
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
  // Live retention can invalidate an otherwise fresh cursor mid-stream.
  const race = fixture(); let raceCalls = 0;
  race.client.ledgerService.listEvents = request => {
    race.calls.push(request);
    return { responses: (async function* () {
      if (++raceCalls === 1) {
        yield event(1); race.floor(8n);
        throw Error('requested data below earliest available; lowest available tx_seq is 42');
      }
      yield event(1); yield event(9); yield end('race-tip', 9n);
    })() };
  };
  result = await history.incrementalEvents(race.client, '0x1::test::RetentionRace');
  assert.deepEqual(result.map(e => e.transactionDigest), ['digest9', 'digest1']);
  assert.equal(race.calls[1].startCheckpoint, 8n);
  assert.equal(race.calls[1].options.after, undefined);
  assert.match(history.historySnapshot(), /retention advanced during discovery/);
  const pruned = fixture(); let attempts = 0;
  pruned.client.ledgerService.listEvents = () => ({ responses: (async function* () {
    attempts++; throw Error('requested data below earliest available');
  })() });
  await assert.rejects(history.incrementalEvents(pruned.client, '0x1::test::StillPruned'), /below earliest/);
  assert.equal(attempts, 4, 'pruning recovery is bounded and never returns empty success');
  // Transaction pruning ahead of the reported checkpoint floor: the floor
  // checkpoint itself is partly pruned, so discovery resumes after it.
  const ahead = fixture(); ahead.floor(20n);
  ahead.client.ledgerService.listEvents = request => {
    ahead.calls.push(request);
    return { responses: (async function* () {
      if (request.startCheckpoint <= 20n) throw Error('requested data below earliest available; lowest available tx_seq is 4135849854');
      yield event(21); yield end('ahead-tip', 21n);
    })() };
  };
  result = await history.incrementalEvents(ahead.client, '0x1::test::TxPrunedFloor');
  assert.deepEqual(result.map(e => e.transactionDigest), ['digest21']);
  assert.deepEqual(ahead.calls.map(c => c.startCheckpoint), [20n, 21n]);
  assert.match(history.historySnapshot(), /History unavailable before checkpoint 21/);
  const quota = fixture(); quota.frames([end('quota', 1n)]);
  window.localStorage.setItem = () => { throw Error('quota exceeded'); };
  assert.deepEqual(await history.incrementalEvents(quota.client, '0x1::test::Quota'), []);
  assert.match(history.historySnapshot(), /storage is full/);
  window.localStorage.setItem = (k,v) => store.set(k,v);
  const paged = fixture(); let page = 0;
  paged.client.ledgerService.listEvents = request => { paged.calls.push(request); return { responses: (async function* () { page++; yield end(`page${page}`, BigInt(page), page < 250 ? 2 : 5); })() }; };
  await history.incrementalEvents(paged.client, '0x1::test::Paged');
  assert.equal(page, 200); assert.match(history.historySnapshot(), /still loading/);
  history = loader()('lib/event-history.ts');
  await history.incrementalEvents(paged.client, '0x1::test::Paged');
  assert.equal(page, 250);
  assert.equal(new TextDecoder().decode(paged.calls[200].options.after), 'page200');
  assert.doesNotMatch(history.historySnapshot(), /still loading/);
  const corruptKey = [...store.keys()].find(k => k.includes('Paged')); store.set(corruptKey, '{bad');
  history = loader()('lib/event-history.ts'); const recovered = fixture(); recovered.frames([end('recovered', 251n)]);
  await history.incrementalEvents(recovered.client, '0x1::test::Paged');
  assert.equal(recovered.calls[0].options.after, undefined);
  for (let i=0;i<12;i++) {
    const large = fixture(); const e = event(i+1); e.event.contents.value = new Uint8Array(100000);
    large.frames([e, end(`large${i}`, 300n)]);
    await history.incrementalEvents(large.client, `0x1::test::Large${i}`);
  }
  assert.ok([...store].reduce((n,[k,v])=>n+2*(k.length+v.length),0) <= 2*1024*1024, 'origin budget shared across streams');
  const huge=fixture(), e=event(1);e.event.contents.value=new Uint8Array(300000);huge.frames([e,end('huge',400n)]);
  assert.equal((await history.incrementalEvents(huge.client,'0x1::test::Huge')).length,1);
  assert.equal([...store.keys()].some(k => k.includes('::Huge:')), false);
  huge.frames([end('huge2',401n)]);
  assert.equal((await history.incrementalEvents(huge.client,'0x1::test::Huge')).length,1, 'oversized events survive later refresh');
  assert.match(history.historySnapshot(), /capacity reached/);
  // Foreign 1,000/3,000-target deployments must never evict a live victim.
  const { deployment_manager_DeploymentCreatedBcs: deployment } = loader()('lib/fleet-protocol.ts');
  const deployments = fixture();
  const created = (n, count) => {
    const frame = event(n);
    frame.event.contents.value = deployment.serialize({ deployment_id: `0x${n}`, creator: '0xa',
      component_name: 'foreign', version: '1', recipe_blob_id: 'blob', artifact_sha256: 'digest',
      target_groups: [], target_devices: Array(count).fill('0xb') }).toBytes();
    return frame;
  };
  const type = '0x1::deployment_manager::DeploymentCreated';
  deployments.frames([created(1, 0), created(2, 1000), created(3, 1000), created(4, 1000), created(5, 3000), end('deployments',500n)]);
  result = await history.incrementalEvents(deployments.client, type);
  assert.equal(result.length, 5);
  assert.ok(result.every(e => e.bcs.length < 100), 'only compact discovery bytes retained');
  deployments.frames([end('deployments2',501n)]);
  assert.equal((await history.incrementalEvents(deployments.client,type)).length,5);
  history = loader()('lib/event-history.ts');
  const reloadDeployments = fixture(); reloadDeployments.frames([end('deployments3',502n)]);
  result = await history.incrementalEvents(reloadDeployments.client,type);
  assert.equal(result.length,5, 'victim and foreign deployments survive reload');
  assert.equal(deployment.parse(result[4].bcs).deployment_id.slice(-1),'1');
  const legacyKey = [...store.keys()].find(k => k.includes(type));
  const legacy = JSON.parse(store.get(legacyKey)); legacy.version = 1; store.set(legacyKey, JSON.stringify(legacy));
  history = loader()('lib/event-history.ts');
  const legacyRescan = fixture(); legacyRescan.frames([created(1,0),end('legacy',503n)]);
  await history.incrementalEvents(legacyRescan.client,type);
  assert.equal(legacyRescan.calls[0].options.after,undefined,'old potentially truncated caches rescan');
  // Numerous compact events may exceed persistence capacity, but never the result.
  const flood = fixture();
  flood.frames([...Array.from({length: 1500}, (_, i) => created(i + 1, 0)), end('flood',2000n)]);
  result = await history.incrementalEvents(flood.client,type);
  assert.equal(result.length,1500);
  assert.ok(result.some(e => deployment.parse(e.bcs).deployment_id.endsWith('0001')));
  assert.equal([...store.keys()].some(k => k.includes(type)),false, 'overflow removes stale persisted cursor');
  flood.frames([end('flood2',2001n)]);
  assert.equal((await history.incrementalEvents(flood.client,type)).length,1500);
  assert.match(history.historySnapshot(), /capacity reached/);
  history = loader()('lib/event-history.ts');
  const rescan = fixture(); rescan.frames([created(1,0),end('rescan',2002n)]);
  assert.equal((await history.incrementalEvents(rescan.client,type)).length,1);
  assert.equal(rescan.calls[0].options.after,undefined,'overflow reload rescans retained range');
  console.log('PASS: type/sender filters; persisted cursor+BCS; reload, empty scan progress, concurrency, pruned/corrupt cache recovery, 250-page reload resumption, origin/stream capacity, bounded mid-stream pruning recovery and nonfatal storage failures');
})().catch(e => { console.error(e); process.exitCode = 1; });
