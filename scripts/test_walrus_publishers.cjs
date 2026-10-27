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
  const deadlines = [];
  const originalTimeout = AbortSignal.timeout;
  AbortSignal.timeout = ms => { deadlines.push(ms); return originalTimeout(ms); };
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
  assert.deepEqual(deadlines, [121000, 121000, 121000]);
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
  await assert.rejects(load().uploadArtifact(file(), null, { kind: 'public' }), /Upload to/);
  assert.equal(calls.length, 1);
  for (const config of [{ ...constants, NETWORK: 'mainnet' }, { ...constants, WALRUS_PUBLISHER: 'https://custom.invalid' }]) {
    calls = [];
    const previousDeadlines = deadlines.length;
    global.fetch = async (url, init) => { assert.equal(init.signal, undefined); calls.push(url); return new Response('failure', { status: 500 }); };
    await assert.rejects(load(config).uploadArtifact(file(), null, { kind: 'public' }));
    assert.equal(calls.length, 1);
    assert.equal(deadlines.length, previousDeadlines);
  }
  // Large Testnet transfers get transfer time in addition to certification time.
  global.fetch = async () => response();
  await load().uploadArtifact({ arrayBuffer: async () => new ArrayBuffer(200 * 1024 * 1024) }, null, { kind: "public" });
  assert.equal(deadlines.at(-1), 1720000);
  AbortSignal.timeout = originalTimeout;
  let uploads = [];
  global.XMLHttpRequest = class {
    upload = new EventTarget();
    open(method, url) { this.url = url; }
    setRequestHeader() {}
    send(body) {
      uploads.push({ url: this.url, body, timeout: this.timeout });
      this.status = uploads.length < 3 ? 500 : 200;
      this.responseText = this.status === 200 ? JSON.stringify({ alreadyCertified: { blobId: 'xhr' } }) : 'WAL exhausted';
      queueMicrotask(() => this.onload());
    }
  };
  const progress = [];
  assert.equal((await load().uploadArtifact(file(), '0xa', { kind: 'public' }, p => progress.push(p))).blobId, 'xhr');
  assert.equal(uploads.length, 3);
  assert.equal(progress.filter(p => p.phase === 'complete').length, 1);
  for (const c of uploads) {
    assert.deepEqual(new Uint8Array(await c.body.arrayBuffer()), bytes);
    assert.equal(c.timeout, 121000);
  }
  for (const config of [{ ...constants, NETWORK: "mainnet" }, { ...constants, WALRUS_PUBLISHER: "https://custom.invalid" }]) {
    uploads = [];
    await assert.rejects(load(config).uploadArtifact(file(), null, { kind: "public" }, () => {}), /Upload to/);
    assert.equal(uploads.length, 1);
    assert.equal(uploads[0].timeout, undefined);
  }
  uploads = [];
  global.XMLHttpRequest.prototype.send = function () { uploads.push(this); queueMicrotask(() => this.onabort()); };
  await assert.rejects(load().uploadArtifact(file(), null, { kind: 'public' }, () => {}), /cancelled/);
  assert.equal(uploads.length, 1);
  delete global.XMLHttpRequest;
  const configured = load({ ...constants, WALRUS_PUBLISHERS: ['https://first.invalid', 'https://second.invalid'], WALRUS_AGGREGATORS: ['https://read1.invalid', 'https://read2.invalid'] });
  calls = [];
  global.fetch = async url => { calls.push(url); return calls.length === 1 ? new Response('WAL exhausted', { status: 503 }) : response(); };
  const configuredResult = await configured.uploadArtifact(file(), null, { kind: 'public' });
  assert.equal(configuredResult.storage.publisherUrl, 'https://second.invalid');
  assert.deepEqual(calls.map(url => new URL(url).origin), ['https://first.invalid', 'https://second.invalid']);
  calls = [];
  global.fetch = async url => { calls.push(url); return new Response('raw private diagnostics', { status: 500 }); };
  await assert.rejects(configured.uploadArtifact(file(), null, { kind: 'public' }), error => {
    assert.match(error.message, /add another publisher/);
    assert.doesNotMatch(error.message, /raw private/);
    assert.match(error.details, /first.invalid.*500/);
    assert.match(error.details, /raw private diagnostics/);
    return true;
  });
  calls = [];
  global.fetch = async url => { calls.push(url); return calls.length === 1 ? new Response('missing', { status: 404 }) : new Response('blob'); };
  assert.equal(await configured.probeBlob('blob'), 'available');
  assert.equal(configured.blobReadEndpoints.get('blob'), 'https://read2.invalid');
  assert.equal(configured.blobDownloadUrl('blob'), 'https://read2.invalid/v1/blobs/blob');
  assert.equal(calls.length, 2);
  calls = [];
  global.fetch = async url => { calls.push(url); return calls.length === 1 ? new Response('gateway failed', { status: 502 }) : new Response('archive'); };
  assert.equal(await (await configured.aggregatorFetch('https://read1.invalid/v1/blobs/archive')).text(), 'archive');
  assert.equal(configured.blobReadEndpoints.get('archive'), 'https://read2.invalid');
  const cancelled = new AbortController(); cancelled.abort();
  const beforeCancel = calls.length;
  await assert.rejects(configured.fetchBlob('cancelled', { signal: cancelled.signal }), /abort/i);
  assert.equal(calls.length, beforeCancel);
  calls = [];
  global.fetch = async () => new Response('failure', { status: (calls.push(1), calls.length === 1 ? 500 : 404) });
  assert.equal(await configured.probeBlob('uncertain'), 'unknown');
  global.fetch = async () => new Response('missing', { status: 404 });
  assert.equal(await configured.probeBlob('missing'), 'unavailable');
  for (const url of ['javascript:alert(1)', 'https://user:secret@example.com', 'https://example.com?token=secret', 'http://public.example']) assert.throws(() => configured.addSessionPublisher(url));
  const savedSession = new Map();
  global.sessionStorage = { getItem: key => savedSession.get(key) ?? null, setItem: (key, value) => savedSession.set(key, value) };
  configured.addSessionPublisher('https://operator.invalid/');
  const saveSession = global.sessionStorage.setItem;
  global.sessionStorage.setItem = () => { throw new Error('Storage denied'); };
  assert.throws(() => configured.addSessionPublisher('https://unsaved.invalid'), /Storage denied/);
  global.sessionStorage.setItem = saveSession;
  calls = [];
  global.fetch = async url => { calls.push(url); return response(); };
  assert.equal((await configured.uploadArtifact(file(), null, { kind: 'public' })).storage.publisherUrl, 'https://operator.invalid');
  const reloaded = load({ ...constants, WALRUS_PUBLISHERS: ['https://first.invalid'] });
  assert.equal((await reloaded.uploadArtifact(file(), null, { kind: 'public' })).storage.publisherUrl, 'https://operator.invalid');
  delete global.sessionStorage;
  const { renderToStaticMarkup } = require('../dapp/node_modules/react-dom/server');
  const { PublisherFailure } = loader({ '@/lib/walrus': configured })('components/PublisherFailure.tsx');
  const html = renderToStaticMarkup(require('../dapp/node_modules/react').createElement(PublisherFailure, { error: new configured.PublisherError('https://failed.invalid', '<raw failure>') }));
  assert.match(html, /add another publisher/);
  assert.match(html, /<details><summary>Technical details<\/summary><pre[^>]*>&lt;raw failure&gt;<\/pre><\/details>/);
  console.log('PASS: bounded fetch/XHR Testnet publisher fallback, unchanged encrypted bytes/owner, fail-closed errors, custom/mainnet isolation and cancellation');
})().catch(error => { console.error(error); process.exitCode = 1; });
