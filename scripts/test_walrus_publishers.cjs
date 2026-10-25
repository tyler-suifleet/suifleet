const assert = require('node:assert/strict');
const { loader } = require('./frontend_test_utils.cjs');
const primary = 'https://publisher.walrus-testnet.walrus.space';
const constants = { NETWORK: 'testnet', WALRUS_PUBLISHER: primary, WALRUS_EPOCHS: 1 };
const load = (config = constants) => loader({ './constants': config })('lib/walrus.ts');
const bytes = new Uint8Array([1, 2, 3]);
const file = () => new File([bytes], 'a.bin');
const response = () => Response.json({ alreadyCertified: { blobId: 'stored' } });
(async () => {
  let calls = [];
  global.fetch = async (url, init) => {
    calls.push({ url, body: new Uint8Array(await init.body.arrayBuffer()) });
    return calls.length < 3 ? new Response('publisher WAL exhausted', { status: 500 }) : response();
  };
  const encrypted = new TextEncoder().encode('suifleet/seal/v1\nopaque-encrypted-envelope');
  let seals = 0;
  const result = await load().uploadArtifact(file(), '0xa', { kind: 'confidential', seal: async () => { seals++; return encrypted; } });
  assert.equal(result.blobId, 'stored');
  assert.equal(seals, 1);
  assert.equal(calls.length, 3);
  assert.equal(new Set(calls.map(c => new URL(c.url).origin)).size, 3);
  for (const c of calls) { assert.deepEqual(c.body, encrypted); assert.match(c.url, /epochs=1&send_object_to=0xa/); }
  for (const status of [400, 403, 429, 500, 503]) {
    calls = [];
    global.fetch = async url => { calls.push(url); return new Response('failure', { status }); };
    await assert.rejects(load().uploadArtifact(file(), null, { kind: 'public' }));
    assert.equal(calls.length, status >= 500 ? 3 : 1);
  }
  calls = [];
  global.fetch = async url => { calls.push(url); if (calls.length === 1) throw new TypeError('network'); return response(); };
  await load().uploadArtifact(file(), null, { kind: 'public' });
  assert.equal(calls.length, 2);
  calls = [];
  global.fetch = async url => { calls.push(url); return Response.json({}); };
  await assert.rejects(load().uploadArtifact(file(), null, { kind: 'public' }), /Unexpected/);
  assert.equal(calls.length, 1);
  for (const config of [{ ...constants, NETWORK: 'mainnet' }, { ...constants, WALRUS_PUBLISHER: 'https://custom.invalid' }]) {
    calls = [];
    global.fetch = async url => { calls.push(url); return new Response('failure', { status: 500 }); };
    await assert.rejects(load(config).uploadArtifact(file(), null, { kind: 'public' }));
    assert.equal(calls.length, 1);
  }
  let uploads = [];
  global.XMLHttpRequest = class {
    upload = new EventTarget();
    open(method, url) { this.url = url; }
    setRequestHeader() {}
    send(body) {
      uploads.push({ url: this.url, body });
      this.status = uploads.length < 3 ? 500 : 200;
      this.responseText = this.status === 200 ? JSON.stringify({ alreadyCertified: { blobId: 'xhr' } }) : 'WAL exhausted';
      queueMicrotask(() => this.onload());
    }
  };
  const progress = [];
  assert.equal((await load().uploadArtifact(file(), '0xa', { kind: 'public' }, p => progress.push(p))).blobId, 'xhr');
  assert.equal(uploads.length, 3);
  assert.equal(progress.filter(p => p.phase === 'complete').length, 1);
  for (const c of uploads) assert.deepEqual(new Uint8Array(await c.body.arrayBuffer()), bytes);
  uploads = [];
  global.XMLHttpRequest.prototype.send = function () { uploads.push(this); queueMicrotask(() => this.onabort()); };
  await assert.rejects(load().uploadArtifact(file(), null, { kind: 'public' }, () => {}), /cancelled/);
  assert.equal(uploads.length, 1);
  console.log('PASS: bounded fetch/XHR Testnet publisher fallback, unchanged encrypted bytes/owner, fail-closed errors, custom/mainnet isolation and cancellation');
})().catch(error => { console.error(error); process.exitCode = 1; });
