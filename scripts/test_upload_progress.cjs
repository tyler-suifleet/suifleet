const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = fs.readFileSync(path.join(__dirname, '../manifests/fixture.localnet.json'), 'utf8');
const { loader } = require('./frontend_test_utils.cjs');
const { uploadArtifact } = loader()('lib/walrus.ts');
const pending = [];
class Request {
  upload = new EventTarget();
  headers = {};
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(key, value) { this.headers[key] = value; }
  send(body) { this.body = body; pending.push(this); }
  emit(type, values = {}) { const event = new Event(type); Object.assign(event, values); this.upload.dispatchEvent(event); }
  complete(status, body) { this.status = status; this.responseText = JSON.stringify(body); this.onload(); }
}
global.XMLHttpRequest = Request;
async function started(promise) {
  for (let i = 0; i < 100 && !pending.length; i++) await new Promise(resolve => setTimeout(resolve, 5));
  assert.ok(pending.length, 'upload was not started');
  return pending.shift();
}
(async () => {
  const progress = [];
  const bytes = new Uint8Array([1, 2, 3, 4]);
  const promise = uploadArtifact(new File([bytes], 'test.bin'), '0xa', { kind: 'public' }, p => progress.push(p));
  const request = await started(promise);
  assert.equal(request.method, 'PUT');
  assert.match(request.url, /send_object_to=0xa/);
  assert.equal(request.headers['Content-Type'], 'application/octet-stream');
  assert.deepEqual(new Uint8Array(await request.body.arrayBuffer()), bytes);
  request.emit('progress', { loaded: 2, total: 4, lengthComputable: true });
  assert.deepEqual(progress.at(-1), { phase: 'uploading', loaded: 2, total: 4 });
  request.emit('progress', { loaded: 3, total: 0, lengthComputable: false });
  assert.equal(progress.at(-1).total, undefined);
  request.emit('load');
  assert.equal(progress.at(-1).phase, 'processing');
  let settled = false; promise.then(() => { settled = true; });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(settled, false, 'sending bytes must not complete a Walrus upload');
  request.complete(200, { newlyCreated: { blobObject: { blobId: 'certified', storage: { endEpoch: 42 } }, cost: 12 } });
  assert.equal((await promise).blobId, 'certified');
  assert.equal(progress.at(-1).phase, 'complete');
  const ciphertext = new TextEncoder().encode('suifleet/seal/v1\nopaque-encrypted-test-envelope');
  const confidential = uploadArtifact(new File([bytes], 'secret.bin'), null,
    { kind: 'confidential', seal: async () => ciphertext }, () => {});
  const encrypted = await started(confidential);
  assert.deepEqual(new Uint8Array(await encrypted.body.arrayBuffer()), ciphertext);
  encrypted.complete(200, { alreadyCertified: { blobId: 'encrypted' } });
  assert.equal((await confidential).confidential, true);
  const before = pending.length;
  await assert.rejects(uploadArtifact(new File([bytes], 'secret.bin'), null,
    { kind: 'confidential', seal: async () => { throw Error('key server unavailable'); } }, () => {}));
  assert.equal(pending.length, before, 'encryption failure must not start a public request');
  for (const mode of ['http', 'invalid', 'network', 'abort', 'timeout']) {
    const updates = [];
    const result = uploadArtifact(new File([bytes], 'test.bin'), null, { kind: 'public' }, p => updates.push(p));
    const rejected = assert.rejects(result);
    const upload = await started(result);
    if (mode === 'http') upload.complete(503, { error: 'unavailable' });
    if (mode === 'invalid') upload.complete(200, {});
    if (mode === 'network') upload.onerror();
    if (mode === 'abort') upload.onabort();
    if (mode === 'timeout') upload.ontimeout();
    await rejected;
    assert.ok(!updates.some(p => p.phase === 'complete'), mode);
  }
  console.log('PASS: actual byte progress, unknown total, delayed certification, unchanged body and HTTP/network/abort/timeout failures');
})().catch(error => { console.error(error); process.exitCode = 1; });
