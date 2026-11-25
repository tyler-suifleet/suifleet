#!/usr/bin/env node
// Real-daemon scenario driver for scripts/test_ipc_node.py. The harness runs it
// as a distinct Unix UID with one JSON argument and reads one JSON line per
// phase. It uses only the public package entry point.
import { createInterface } from 'node:readline';
import { BufferFullError, Client, DataPutError, OutboundBuffer } from '@suifleet/component-ipc';

const spec = JSON.parse(process.argv[2]);
const client = new Client({ socketPath: spec.socket, component: spec.component ?? '', timeoutMs: spec.timeoutMs ?? 30_000,
  connectAttempts: spec.connectAttempts ?? 1, connectRetryDelayMs: spec.connectRetryDelayMs ?? 250 });
const describe = (error) => ({ name: error?.name, code: error?.code, outcome: error?.outcome, errno: error?.errno,
  oldest: error?.oldest, message: String(error?.message ?? error) });
const emit = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
const lines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();

switch (spec.action) {
  case 'call': {
    const started = Date.now();
    try {
      emit({ ok: true, data: await client.request(spec.operation, { topic: spec.topic ?? '', payload: spec.payload ?? null }),
        elapsed_ms: Date.now() - started });
    } catch (error) {
      emit({ ok: false, error: describe(error), elapsed_ms: Date.now() - started });
    }
    break;
  }
  case 'buffer': {
    // Queue items beyond the daemon's bound, flush, wait for the harness to
    // drain the destination, then flush the remainder.
    const buffer = new OutboundBuffer(client, { maxItems: spec.maxItems, maxBytes: spec.maxBytes ?? 1 << 20 });
    let overflow = 0;
    for (const item of spec.items) {
      try {
        if (item.kind === 'publish') buffer.publish(item.topic, item.payload); else buffer.append(item.stream, item.payload);
      } catch (error) {
        if (!(error instanceof BufferFullError)) throw error;
        overflow++;
      }
    }
    for (let phase = 0; phase < (spec.phases ?? 2); phase++) {
      if (phase > 0) await lines.next();
      const result = await buffer.flush({ maxAttempts: spec.maxAttempts ?? 2, retryDelayMs: 50 });
      emit({ phase, overflow, sent: result.sent, pending: result.pending, error: result.error ? describe(result.error) : null });
    }
    break;
  }
  case 'put_data': {
    try {
      emit({ ok: true, data: await client.putData(spec.stream, 'sample', Buffer.alloc(spec.size, 3)) });
    } catch (error) {
      emit({ ok: false, error: describe(error), chunk: error instanceof DataPutError ? error.chunk : null,
        upload: error instanceof DataPutError ? error.upload : null, cause: describe(error.cause) });
    }
    break;
  }
  default:
    throw new Error(`unknown action ${spec.action}`);
}
process.exit(0);
