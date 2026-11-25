// Client framing, outcome classification and buffering against a local fake
// Unix-socket peer. Real-daemon behavior is covered by scripts/test_ipc_node.py.
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import {
  BufferFullError, Client, DATA_CHUNK_SIZE, DataPutError, IpcError, MAX_FRAME, OutboundBuffer, ProtocolError,
  TransportError, UncertainDeliveryError, encodeRequest, validateDataMetadata,
} from '../dist/index.js';

const directory = mkdtempSync(join(tmpdir(), 'sf302-unit-'));
after(() => rmSync(directory, { recursive: true, force: true }));
let counter = 0;

/** Fake daemon: `handle(request, socket)` returns a response object, a raw string/Buffer, or undefined. */
async function serve(handle) {
  const path = join(directory, `s${counter++}.sock`);
  const requests = [];
  const server = createServer((socket) => {
    let data = '';
    socket.on('data', async (chunk) => {
      data += chunk;
      if (!data.includes('\n')) return;
      const request = JSON.parse(data.slice(0, data.indexOf('\n')));
      requests.push(request);
      const reply = await handle(request, socket);
      if (reply === undefined) return;
      socket.end(typeof reply === 'string' || Buffer.isBuffer(reply) ? reply : `${JSON.stringify(reply)}\n`);
    });
    socket.on('error', () => {});
  });
  await new Promise((resolve) => server.listen(path, resolve));
  return { path, requests, close: () => new Promise((resolve) => server.close(resolve)) };
}
const ok = (data) => ({ version: 1, ok: true, data });

test('request frame is the v1 wire object', () => {
  const frame = encodeRequest('publish', 'svc', 'svc/events', { t: 1 });
  assert.equal(frame.toString(), '{"version":1,"operation":"publish","component":"svc","topic":"svc/events","payload":{"t":1}}\n');
  assert.equal(encodeRequest('list', '', '', undefined).toString(), '{"version":1,"operation":"list","component":"","topic":"","payload":null}\n');
  assert.throws(() => encodeRequest('publish', 'svc', 't', 'x'.repeat(MAX_FRAME)), RangeError);
});

test('named methods send the documented operation, topic and payload', async () => {
  const server = await serve((r) => ok(
    r.operation === 'config' ? { configuration: { effective: { a: 1 } } }
      : r.operation === 'stream_append' ? { sequence: 7 }
        : r.operation === 'stream_read' ? { messages: [{ sequence: 0, appended_ms: 1, payload: 'x' }] }
          : r.operation === 'logs' ? { tail: 'log' } : { echoed: true }));
  const client = new Client({ socketPath: server.path, component: 'svc' });
  assert.deepEqual(await client.configuration(), { effective: { a: 1 } });
  const unconfigured = await serve(() => ok({ configuration: null, services: {} }));
  assert.equal(await new Client({ socketPath: unconfigured.path, component: 'svc' }).configuration(), null);
  await unconfigured.close();
  assert.equal(await client.streamAppend('s', { v: 1 }), 7);
  assert.equal((await client.streamRead('s', { start: 3, minMessages: 1, maxMessages: 9 })).length, 1);
  assert.equal(await client.logs(), 'log');
  await client.health('healthy');
  await client.subscribe('a/#', 0);
  await client.ack('a/#', 12);
  await client.streamCreate('s', { maxSize: 4096, timeToLiveSeconds: 60 });
  await client.shadowUpdate('cfg', { reported: { m: 1 } }, { version: 3, clientToken: 't' });
  await client.shadowDelete('cfg');
  await client.request('list', { component: '' });
  const sent = server.requests.map(({ operation, component, topic, payload }) => [operation, component, topic, payload]);
  assert.deepEqual(sent, [
    ['config', 'svc', '', null],
    ['stream_append', 'svc', 's', { v: 1 }],
    ['stream_read', 'svc', 's', { start: 3, min: 1, max: 9 }],
    ['logs', 'svc', '', null],
    ['health', 'svc', '', 'healthy'],
    ['subscribe', 'svc', 'a/#', { qos: 0 }],
    ['ack', 'svc', 'a/#', 12],
    ['stream_create', 'svc', 's', { max_size: 4096, strategy_on_full: 'RejectNewData', persistence: 'File', time_to_live_seconds: 60 }],
    ['shadow_update', 'svc', 'cfg', { state: { reported: { m: 1 } }, version: 3, clientToken: 't' }],
    ['shadow_delete', 'svc', 'cfg', null],
    ['list', '', '', null],
  ]);
  for (const request of server.requests) assert.deepEqual(Object.keys(request), ['version', 'operation', 'component', 'topic', 'payload']);
  await server.close();
});

test('daemon rejections become IpcError with protocol codes', async () => {
  const server = await serve((r) => r.topic === 'old'
    ? { version: 1, ok: false, error: 'evicted', oldest: 40 } : { version: 1, ok: false, error: 'request_failed' });
  const client = new Client({ socketPath: server.path, component: 'svc' });
  await assert.rejects(client.status(), (e) => e instanceof IpcError && e.code === 'request_failed' && e.operation === 'status');
  await assert.rejects(client.streamRead('old'), (e) => e instanceof IpcError && e.code === 'evicted' && e.oldest === 40);
  await server.close();
});

test('unsupported, malformed and oversize responses are protocol errors with unknown outcome', async () => {
  const replies = [
    { version: 2, ok: true, data: 1 },
    'not json\n',
    { version: 1, data: 1 },
    { version: 1, ok: true },
    `${'x'.repeat(MAX_FRAME + 10)}\n`,
  ];
  for (const reply of replies) {
    const server = await serve(() => reply);
    const client = new Client({ socketPath: server.path });
    await assert.rejects(client.list(), (e) => e instanceof ProtocolError && e.outcome === 'unknown', JSON.stringify(reply).slice(0, 40));
    await server.close();
  }
});

test('responses split across reads are reassembled; bytes after LF are ignored', async () => {
  const server = await serve((_, socket) => {
    const line = `${JSON.stringify(ok({ big: 'y'.repeat(30000) }))}\n`;
    socket.write(line.slice(0, 10));
    setTimeout(() => socket.end(`${line.slice(10)}trailing`), 20);
  });
  assert.equal((await new Client({ socketPath: server.path }).list()).big.length, 30000);
  await server.close();
});

test('a closed or silent connection after sending has an unknown outcome and is never retried', async () => {
  const closed = await serve((_, socket) => { socket.destroy(); });
  const client = new Client({ socketPath: closed.path, connectAttempts: 5, connectRetryDelayMs: 1 });
  await assert.rejects(client.stop(), (e) => e instanceof TransportError && e.outcome === 'unknown');
  assert.equal(closed.requests.length, 1, 'a request that may have executed was resent');
  await closed.close();
  const silent = await serve(() => undefined);
  const slow = new Client({ socketPath: silent.path, timeoutMs: 100, connectAttempts: 5 });
  await assert.rejects(slow.restart(), (e) => e instanceof TransportError && e.outcome === 'unknown' && /timed out/.test(e.message));
  assert.equal(silent.requests.length, 1);
  await silent.close();
});

test('a missing socket is not_sent; connect retries span a restart and send exactly once', async () => {
  const path = join(directory, 'later.sock');
  await assert.rejects(new Client({ socketPath: path }).status(),
    (e) => e instanceof TransportError && e.outcome === 'not_sent' && e.errno === 'ENOENT');
  const client = new Client({ socketPath: path, connectAttempts: 8, connectRetryDelayMs: 20 });
  let requests = 0;
  const pending = client.status();
  await new Promise((resolve) => setTimeout(resolve, 60));
  const server = createServer((socket) => socket.once('data', () => { requests++; socket.end(`${JSON.stringify(ok({ phase: 'running' }))}\n`); }));
  await new Promise((resolve) => server.listen(path, resolve));
  assert.deepEqual(await pending, { phase: 'running' });
  assert.equal(requests, 1);
  await new Promise((resolve) => server.close(resolve));
});

test('abort before sending is not_sent', async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(new Client({ socketPath: join(directory, 'none.sock') }).request('list', { signal: controller.signal }),
    (e) => e instanceof TransportError && e.outcome === 'not_sent');
});

test('abort during connection retry backoff is not_sent with the abort reason as cause', async () => {
  const controller = new AbortController();
  const reason = new Error('caller cancelled');
  const client = new Client({ socketPath: join(directory, 'missing.sock'), connectAttempts: 2, connectRetryDelayMs: 1000 });
  const started = Date.now();
  setTimeout(() => controller.abort(reason), 80);
  await assert.rejects(client.request('stream_append', { signal: controller.signal }),
    (e) => e instanceof TransportError && e.outcome === 'not_sent' && e.cause === reason);
  assert.ok(Date.now() - started < 900, 'abort interrupts the backoff instead of waiting it out');
});

test('one request deadline covers connect backoff: a late listener never receives the request', async (t) => {
  const path = join(directory, 'late-deadline.sock');
  const client = new Client({ socketPath: path, timeoutMs: 30, connectAttempts: 2, connectRetryDelayMs: 150 });
  let requests = 0;
  const server = createServer((socket) => socket.once('data', () => { requests++; socket.end(`${JSON.stringify(ok({ sequence: 1 }))}\n`); }));
  setTimeout(() => server.listen(path), 75);
  t.after(() => new Promise((resolve) => server.close(() => resolve())));
  const started = Date.now();
  await assert.rejects(client.request('stream_append', { topic: 's', payload: { payload: 1 } }),
    (e) => e instanceof TransportError && e.outcome === 'not_sent' && /timed out after 30 ms/.test(e.message));
  assert.ok(Date.now() - started < 140, 'backoff is clamped to the request deadline');
  await new Promise((resolve) => setTimeout(resolve, 200));
  assert.equal(requests, 0, 'a request was dispatched after its deadline');
});

test('a connect callback delayed past the deadline by an event-loop stall sends nothing', async (t) => {
  const path = join(directory, 'stalled-connect.sock');
  const frames = [];
  const sockets = [];
  // The accept handler blocks this process's event loop beyond timeoutMs, so the
  // client's connect callback runs after the deadline with its timer still queued.
  const server = createServer((socket) => {
    sockets.push(socket);
    socket.on('data', (chunk) => frames.push(chunk));
    socket.on('error', () => {});
    const until = performance.now() + 80;
    while (performance.now() < until) { /* stall */ }
  });
  await new Promise((resolve) => server.listen(path, resolve));
  t.after(() => new Promise((resolve) => { for (const socket of sockets) socket.destroy(); server.close(() => resolve()); }));
  const client = new Client({ socketPath: path, timeoutMs: 50 });
  await assert.rejects(client.restart(),
    (e) => e instanceof TransportError && e.outcome === 'not_sent' && /timed out after 50 ms/.test(e.message));
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(sockets.length, 1, 'the stalled connection was accepted');
  assert.equal(frames.length, 0, 'a request was written after its deadline');
});

test('the response wait uses only the time left after connect backoff', async (t) => {
  const path = join(directory, 'late-silent.sock');
  const client = new Client({ socketPath: path, timeoutMs: 400, connectAttempts: 3, connectRetryDelayMs: 200 });
  const received = [];
  const sockets = [];
  const server = createServer((socket) => { sockets.push(socket); socket.once('data', (chunk) => { received.push(chunk); }); });
  setTimeout(() => server.listen(path), 50);
  t.after(() => new Promise((resolve) => { server.close(() => resolve()); for (const socket of sockets) socket.destroy(); }));
  const started = Date.now();
  await assert.rejects(client.restart(),
    (e) => e instanceof TransportError && e.outcome === 'unknown' && /timed out/.test(e.message));
  const elapsed = Date.now() - started;
  assert.equal(received.length, 1);
  assert.ok(elapsed >= 390 && elapsed < 560, `whole request ended at ${elapsed} ms, not a fresh per-exchange timeout`);
});

test('publish payloads above 4096 serialized bytes are refused locally', async () => {
  const client = new Client({ socketPath: join(directory, 'none.sock') });
  await assert.rejects(client.publish('t', 'x'.repeat(4095)), RangeError);
});

test('putData chunks, hashes and resumes with a stable upload ID', async () => {
  let fail = 2;
  const server = await serve((r) => r.payload.descriptor.chunk === fail
    ? { version: 1, ok: false, error: 'full' } : ok({ sequence: r.payload.descriptor.chunk, state: 'queued', storage_acknowledged: false }));
  const client = new Client({ socketPath: server.path, component: 'svc' });
  const data = Buffer.alloc(DATA_CHUNK_SIZE * 3 + 5, 7);
  const upload = 'ab'.repeat(32);
  const error = await client.putData('sensor', 'k', data, { upload }).catch((e) => e);
  assert.ok(error instanceof DataPutError && error.chunk === 2 && error.upload === upload && error.receipts.length === 2);
  assert.ok(error.cause instanceof IpcError && error.cause.code === 'full');
  fail = -1;
  const resumed = await client.putData('sensor', 'k', data, { upload, startChunk: error.chunk });
  assert.deepEqual(resumed.chunks.map((c) => c.sequence), [2, 3]);
  const last = server.requests.at(-1).payload;
  assert.equal(last.descriptor.size, 5);
  assert.equal(last.descriptor.chunks, 4);
  assert.equal(last.descriptor.total_size, data.length);
  assert.deepEqual(last.descriptor.sha256, [...createHash('sha256').update(data.subarray(DATA_CHUNK_SIZE * 3)).digest()]);
  assert.equal(Buffer.from(last.data_base64, 'base64').length, 5);
  assert.deepEqual(Object.keys(last.descriptor), ['stream', 'key', 'content_type', 'metadata', 'upload', 'chunk', 'chunks', 'total_size', 'size', 'sha256']);
  await assert.rejects(client.putData('sensor', 'k', data, { upload: 'nothex' }), RangeError);
  await server.close();
});

test('putData metadata matches the daemon string map contract and is refused locally otherwise', async () => {
  const server = await serve((r) => ok({ sequence: r.payload.descriptor.chunk, state: 'queued', storage_acknowledged: false }));
  const client = new Client({ socketPath: server.path, component: 'svc' });
  const metadata = { sampleCount: '5', 'unit ☃': 'mV' };
  await client.putData('sensor', 'k', Buffer.from([1, 2]), { metadata });
  assert.deepEqual(server.requests.at(-1).payload.descriptor.metadata, metadata);
  await client.putData('sensor', 'k', Buffer.from([1]));
  assert.deepEqual(server.requests.at(-1).payload.descriptor.metadata, {});
  const sent = server.requests.length;
  const refused = [
    [{ sampleCount: 5 }, TypeError], [{ nested: { a: '1' } }, TypeError], [{ flag: true }, TypeError], [{ none: null }, TypeError],
    [['a'], TypeError], ['text', TypeError], [new Map([['a', 'b']]), TypeError], [{ bad: '\ud800' }, TypeError],
    [{ '': 'x' }, RangeError], [{ ['k'.repeat(65)]: 'x' }, RangeError], [{ k: 'é'.repeat(129) }, RangeError],
    [Object.fromEntries(Array.from({ length: 17 }, (_, i) => [`k${i}`, 'v'])), RangeError],
    // Within per-entry limits, but control characters serialize to \\u0001 (6 bytes) and exceed 6144 serialized bytes.
    [Object.fromEntries(Array.from({ length: 16 }, (_, i) => [`k${i}`, '\u0001'.repeat(100)])), RangeError],
  ];
  for (const [value, kind] of refused) {
    await assert.rejects(client.putData('sensor', 'k', Buffer.from([1]), { metadata: value }), kind, JSON.stringify(String(value)));
  }
  // An explicit top-level null is a non-map, not an omitted value.
  await assert.rejects(client.putData('sensor', 'k', Buffer.from([1]), { metadata: null }), TypeError);
  assert.equal(server.requests.length, sent, 'refused metadata never reaches the daemon');
  // Limits are inclusive; the copy is detached from the caller's object.
  const max = { ['k'.repeat(64)]: 'é'.repeat(128) };
  const copy = validateDataMetadata(max);
  assert.deepEqual(copy, max);
  assert.notEqual(copy, max);
  await server.close();
});

test('CLI data_put refuses non-string metadata before connecting and forwards a string map', async () => {
  const cli = fileURLToPath(new URL('../dist/cli.js', import.meta.url));
  const file = join(directory, 'blob.bin');
  writeFileSync(file, Buffer.from([9, 8, 7]));
  const run = (socket, metadata) => new Promise((resolve) => execFile(process.execPath,
    [cli, '--socket', socket, '--component', 'svc', '--topic', 'sensor', '--file', file, '--metadata', metadata, 'data_put'],
    (error, stdout, stderr) => resolve({ code: error ? error.code : 0, stdout, stderr })));
  const server = await serve((r) => ok({ sequence: 1, state: 'queued', storage_acknowledged: false }));
  const bad = await run(server.path, '{"sampleCount":5}');
  assert.equal(bad.code, 1);
  assert.match(bad.stderr, /metadata value for "sampleCount" must be a string/);
  assert.equal(server.requests.length, 0);
  const good = await run(server.path, '{"sampleCount":"5"}');
  assert.equal(good.code, 0, good.stderr);
  assert.deepEqual(server.requests[0].payload.descriptor.metadata, { sampleCount: '5' });
  await server.close();
});

test('messages() yields durable messages and acks them by id', async () => {
  const queue = [{ id: 4, topic: 'a/b', sender: 'p', payload: 1, published_ms: 1 }];
  const server = await serve((r) => r.operation === 'receive' ? ok(queue[0] ?? null)
    : r.operation === 'ack' ? ok({ acknowledged: queue.shift()?.id === r.payload }) : ok(null));
  const client = new Client({ socketPath: server.path, component: 'sub' });
  const controller = new AbortController();
  const seen = [];
  for await (const delivery of client.messages('a/#', { pollIntervalMs: 10, signal: controller.signal })) {
    seen.push(delivery.message.id);
    assert.equal(await delivery.ack(), true);
    if (seen.length === 1) setTimeout(() => queue.push({ id: 5, topic: 'a/c', sender: 'p', payload: 2, published_ms: 2 }), 30);
    if (seen.length === 2) controller.abort();
  }
  assert.deepEqual(seen, [4, 5]);
  await server.close();
});

test('messages() abort of a sent receive throws unknown; idle or unsent abort ends cleanly', async () => {
  // In flight: the daemon has the receive (and may have popped a message) but has not replied.
  const silent = await serve(() => undefined);
  const inFlight = new AbortController();
  const iterator = new Client({ socketPath: silent.path, component: 'sub' }).messages('a/#', { signal: inFlight.signal });
  const next = iterator.next();
  while (silent.requests.length === 0) await new Promise((resolve) => setTimeout(resolve, 5));
  inFlight.abort();
  try {
    await assert.rejects(next, (e) => e instanceof TransportError && e.outcome === 'unknown' && e.operation === 'receive');
  } finally { await silent.close(); }

  // Idle: the queue is empty and the iterator is sleeping between polls.
  const empty = await serve(() => ok(null));
  const idle = new AbortController();
  const idleNext = new Client({ socketPath: empty.path, component: 'sub' }).messages('a/#', { pollIntervalMs: 10_000, signal: idle.signal }).next();
  while (empty.requests.length === 0) await new Promise((resolve) => setTimeout(resolve, 5));
  await new Promise((resolve) => setTimeout(resolve, 20));
  idle.abort();
  try {
    assert.deepEqual(await idleNext, { value: undefined, done: true });
    assert.equal(empty.requests.length, 1);
  } finally { await empty.close(); }

  // Not sent: no daemon socket, so every attempt fails before a byte is written.
  const unsent = new AbortController();
  const unsentNext = new Client({ socketPath: join(directory, 'absent.sock') })
    .messages('a/#', { pollIntervalMs: 10, maxReconnectDelayMs: 20, signal: unsent.signal }).next();
  setTimeout(() => unsent.abort(), 50);
  assert.deepEqual(await unsentNext, { value: undefined, done: true });

  // Already aborted: no request is made at all.
  const before = new AbortController();
  before.abort();
  assert.deepEqual(await new Client({ socketPath: join(directory, 'absent.sock') }).messages('a/#', { signal: before.signal }).next(),
    { value: undefined, done: true });
});

test('OutboundBuffer is bounded, ordered and retries only definite rejections', async () => {
  let full = 2;
  const server = await serve((r) => {
    if (r.operation === 'publish' && full > 0) { full--; return { version: 1, ok: false, error: 'request_failed' }; }
    return ok(r.operation === 'publish' ? { delivered: 1, durable: 0, volatile: 0 } : { sequence: 0 });
  });
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path, component: 'svc' }), { maxItems: 3, maxBytes: 64 });
  buffer.publish('t', 1);
  buffer.append('s', 2);
  buffer.publish('t', 3);
  assert.throws(() => buffer.publish('t', 4), BufferFullError);
  assert.throws(() => buffer.publish('t', 'x'.repeat(5000)), RangeError);
  const partial = await buffer.flush({ maxAttempts: 2, retryDelayMs: 1 });
  assert.equal(partial.sent, 0);
  assert.equal(partial.pending, 3);
  assert.ok(partial.error instanceof IpcError);
  assert.deepEqual(await buffer.flush({ retryDelayMs: 1 }), { sent: 3, pending: 0 });
  assert.deepEqual(server.requests.map((r) => [r.operation, r.payload]),
    [['publish', 1], ['publish', 1], ['publish', 1], ['stream_append', 2], ['publish', 3]]);
  const small = new OutboundBuffer(new Client({ socketPath: server.path }), { maxBytes: 10 });
  small.publish('t', '1234');
  assert.throws(() => small.publish('t', '12345'), BufferFullError);
  await server.close();
});

test('OutboundBuffer stops on an uncertain delivery until the caller decides', async () => {
  let drop = true;
  const server = await serve((_, socket) => {
    if (drop) { drop = false; socket.destroy(); return undefined; }
    return ok({ delivered: 1, durable: 0, volatile: 0 });
  });
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path }));
  buffer.publish('t', 'a');
  buffer.publish('t', 'b');
  await assert.rejects(buffer.flush({ retryDelayMs: 1 }), (e) => e instanceof UncertainDeliveryError && e.item.payload === 'a');
  assert.equal(buffer.blocked, true);
  await assert.rejects(buffer.flush(), /resolveUncertain/);
  buffer.resolveUncertain('discard');
  assert.deepEqual(await buffer.flush(), { sent: 1, pending: 0 });
  assert.deepEqual(server.requests.map((r) => r.payload), ['a', 'b']);
  await server.close();
});

test('OutboundBuffer never replays a stream_append that may have been stored', async () => {
  // request_failed can follow a stored append (e.g. a failed segment cleanup).
  let generic = true;
  const server = await serve((r) => {
    if (r.operation === 'stream_append' && generic) { generic = false; return { version: 1, ok: false, error: 'request_failed' }; }
    return ok({ sequence: 0 });
  });
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path }));
  buffer.append('s', 'a');
  buffer.append('s', 'b');
  await assert.rejects(buffer.flush({ retryDelayMs: 1 }),
    (e) => e instanceof UncertainDeliveryError && e.item.payload === 'a' && e.cause instanceof IpcError && e.cause.code === 'request_failed');
  assert.equal(buffer.blocked, true);
  assert.equal(buffer.pending, 2);
  assert.deepEqual(server.requests.map((r) => r.payload), ['a']);
  buffer.resolveUncertain('discard');
  assert.deepEqual(await buffer.flush(), { sent: 1, pending: 0 });
  assert.deepEqual(server.requests.map((r) => r.payload), ['a', 'b']);
  await server.close();
});

test('OutboundBuffer retries transient stream codes and stops at once on permanent ones', async () => {
  const replies = ['full', 'unavailable', null, 'not-found'];
  const server = await serve(() => {
    const code = replies.shift();
    return code ? { version: 1, ok: false, error: code } : ok({ sequence: 0 });
  });
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path }));
  buffer.append('s', 1);
  buffer.append('missing', 2);
  const result = await buffer.flush({ maxAttempts: 5, retryDelayMs: 1 });
  assert.equal(result.sent, 1);
  assert.equal(result.pending, 1);
  assert.ok(result.error instanceof IpcError && result.error.code === 'not-found');
  assert.equal(buffer.blocked, false);
  assert.deepEqual(server.requests.map((r) => r.payload), [1, 1, 1, 2]);
  await server.close();
});

test('OutboundBuffer snapshots payloads at enqueue', async () => {
  let refuse = 1;
  const server = await serve((r) => {
    if (r.operation === 'publish' && refuse > 0) { refuse--; return { version: 1, ok: false, error: 'request_failed' }; }
    return ok(r.operation === 'publish' ? { delivered: 1, durable: 0, volatile: 0 } : { sequence: 0 });
  });
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path }), { maxBytes: 40 });
  const reading = { n: 1, tags: ['a'] };
  buffer.publish('t', reading);
  buffer.append('s', reading);
  const bytes = buffer.pendingBytes;
  reading.n = 2;
  reading.tags.push('x'.repeat(100));
  reading.extra = 'y'.repeat(5000);
  assert.equal(buffer.pendingBytes, bytes);
  // The first attempt is refused; the retry still sends the enqueue-time value.
  assert.deepEqual(await buffer.flush({ retryDelayMs: 1 }), { sent: 2, pending: 0 });
  assert.deepEqual(server.requests.map((r) => [r.operation, r.payload]),
    [['publish', { n: 1, tags: ['a'] }], ['publish', { n: 1, tags: ['a'] }], ['stream_append', { n: 1, tags: ['a'] }]]);
  await server.close();
});

test('OutboundBuffer refuses an item whose frame can never be sent, leaving the queue unchanged', async () => {
  const server = await serve(() => ok({ sequence: 0 }));
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path, component: 'svc' }));
  buffer.append('s', 'a');
  const bytes = buffer.pendingBytes;
  assert.throws(() => buffer.append('s', 'x'.repeat(MAX_FRAME)), RangeError);
  // Just under the payload bound but over the frame once the envelope is added.
  assert.throws(() => buffer.append('s', 'x'.repeat(MAX_FRAME - 60)), RangeError);
  assert.equal(buffer.pending, 1);
  assert.equal(buffer.pendingBytes, bytes);
  assert.deepEqual(await buffer.flush(), { sent: 1, pending: 0 });
  assert.deepEqual(server.requests.map((r) => r.payload), ['a']);
  await server.close();
});

test('OutboundBuffer lets the caller drop a definitively rejected head and resume', async () => {
  const server = await serve((r) => (r.payload === 'bad' ? { version: 1, ok: false, error: 'invalid' } : ok({ sequence: 0 })));
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path }));
  assert.throws(() => buffer.discardHead(), /empty/);
  buffer.append('s', 'bad');
  buffer.append('s', 'next');
  const stopped = await buffer.flush({ retryDelayMs: 1 });
  assert.equal(stopped.rejected, true);
  assert.equal(stopped.pending, 2);
  assert.ok(stopped.error instanceof IpcError && stopped.error.code === 'invalid');
  assert.equal(buffer.head.payload, 'bad');
  assert.throws(() => buffer.resolveUncertain('discard'), /no uncertain/);
  assert.equal(buffer.discardHead().payload, 'bad');
  assert.deepEqual(await buffer.flush(), { sent: 1, pending: 0 });
  assert.deepEqual(server.requests.map((r) => r.payload), ['bad', 'next']);
  await server.close();
});

test('OutboundBuffer head inspection cannot change the validated item', async () => {
  const server = await serve(() => ok({ sequence: 0 }));
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path }));
  buffer.append('s', { n: 1 });
  buffer.publish('t', 'second');
  const bytes = buffer.pendingBytes;
  const head = buffer.head;
  assert.ok(Object.isFrozen(head) && Object.isFrozen(head.payload));
  // Strict-mode JavaScript: writes to the frozen item throw and change nothing.
  assert.throws(() => { head.payload = 'x'.repeat(MAX_FRAME); }, TypeError);
  assert.throws(() => { head.stream = 'other'; }, TypeError);
  assert.throws(() => { head.kind = 'publish'; }, TypeError);
  assert.throws(() => { head.payload.n = 2; }, TypeError);
  assert.equal(buffer.pendingBytes, bytes);
  assert.deepEqual(buffer.head, { kind: 'stream', stream: 's', payload: { n: 1 } });
  assert.deepEqual(await buffer.flush(), { sent: 2, pending: 0 });
  assert.deepEqual(server.requests.map((r) => [r.operation, r.topic, r.payload]),
    [['stream_append', 's', { n: 1 }], ['publish', 't', 'second']]);
  await server.close();
});

test('OutboundBuffer keeps uncertain-head discard behind resolveUncertain', async () => {
  const server = await serve((_, socket) => { socket.destroy(); return undefined; });
  const buffer = new OutboundBuffer(new Client({ socketPath: server.path }));
  buffer.publish('t', 'a');
  await assert.rejects(buffer.flush(), UncertainDeliveryError);
  assert.throws(() => buffer.discardHead(), /resolveUncertain/);
  assert.equal(buffer.pending, 1);
  buffer.resolveUncertain('discard');
  assert.equal(buffer.pending, 0);
  await server.close();
});

test('OutboundBuffer flush stops promptly when its signal aborts a nested client retry', async () => {
  const path = join(directory, `late${counter++}.sock`);
  const buffer = new OutboundBuffer(new Client({ socketPath: path, connectAttempts: 2, connectRetryDelayMs: 1000 }));
  buffer.append('s', 'a');
  const controller = new AbortController();
  const reason = new Error('caller cancelled');
  const started = Date.now();
  setTimeout(() => controller.abort(reason), 80);
  await assert.rejects(buffer.flush({ signal: controller.signal, retryDelayMs: 1200 }), (e) => e === reason);
  assert.ok(Date.now() - started < 900, 'abort is not delayed by the buffer retry delay');
  assert.equal(buffer.pending, 1, 'the unsent head stays pending');
  // The flush slot is released at once; the same head is later sent exactly once.
  const requests = [];
  const server = createServer((socket) => {
    socket.on('data', (chunk) => { requests.push(JSON.parse(String(chunk).split('\n')[0])); socket.end(`${JSON.stringify(ok({ seq: 1 }))}\n`); });
    socket.on('error', () => {});
  });
  await new Promise((resolve) => server.listen(path, resolve));
  const before = new AbortController();
  before.abort(reason);
  // Already-aborted signal: refused before any request.
  await assert.rejects(buffer.flush({ signal: before.signal }), (e) => e === reason);
  assert.equal(requests.length, 0);
  assert.deepEqual(await buffer.flush(), { sent: 1, pending: 0 });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].operation, 'stream_append');
  await new Promise((resolve) => server.close(resolve));
});
