#!/usr/bin/env node
/** suifleet-ipc: generic native IPC v1 requests from the shell (same contract as sdk/python/suifleet_ipc.py). */
import { readFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import {
  Client, DEFAULT_SOCKET, DataPutError, IpcError, MAX_DATA_SIZE, OPERATIONS, SDK_VERSION, TransportError, validateDataMetadata,
} from './index.js';
import type { JsonValue } from './index.js';

const usage = `usage: suifleet-ipc [--socket PATH] [--component NAME] [--topic TOPIC] [--payload JSON]
                    [--timeout-ms N] [--connect-attempts N] OPERATION
       suifleet-ipc --component NAME --topic STREAM --file PATH [--key KEY] [--content-type TYPE]
                    [--metadata JSON-OBJECT-OF-STRINGS] [--upload HEX] [--start-chunk N] data_put
operations: ${OPERATIONS.join(', ')}
Identity is this process's Unix UID; --component only names the component it owns.`;

async function main(): Promise<number> {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      socket: { type: 'string', default: DEFAULT_SOCKET },
      component: { type: 'string', default: '' },
      topic: { type: 'string', default: '' },
      payload: { type: 'string' },
      'timeout-ms': { type: 'string', default: '120000' },
      'connect-attempts': { type: 'string', default: '1' },
      file: { type: 'string' },
      key: { type: 'string', default: 'data' },
      'content-type': { type: 'string', default: 'application/octet-stream' },
      metadata: { type: 'string', default: '{}' },
      upload: { type: 'string' },
      'start-chunk': { type: 'string', default: '0' },
      help: { type: 'boolean', short: 'h' },
      version: { type: 'boolean' },
    },
  });
  if (values.help) { process.stdout.write(`${usage}\n`); return 0; }
  if (values.version) { process.stdout.write(`${SDK_VERSION}\n`); return 0; }
  const operation = positionals[0];
  if (positionals.length !== 1 || operation === undefined || !(OPERATIONS as readonly string[]).includes(operation)) {
    process.stderr.write(`${usage}\n`);
    return 2;
  }
  const client = new Client({ socketPath: values.socket, component: values.component,
    timeoutMs: Number(values['timeout-ms']), connectAttempts: Number(values['connect-attempts']) });
  let result: unknown;
  if (values.file !== undefined) {
    if (operation !== 'data_put') throw new RangeError('--file requires data_put');
    // The daemon accepts only a string-to-string map; refuse anything else before connecting.
    const metadata = validateDataMetadata(JSON.parse(values.metadata) as unknown);
    const data = readFileSync(values.file);
    if (data.length > MAX_DATA_SIZE) throw new RangeError('data exceeds 64 MiB');
    result = await client.putData(values.topic, values.key, data, {
      contentType: values['content-type'], metadata,
      startChunk: Number(values['start-chunk']), ...(values.upload !== undefined ? { upload: values.upload } : {}),
    });
  } else {
    result = await client.request(operation, { topic: values.topic,
      ...(values.payload !== undefined ? { payload: JSON.parse(values.payload) as JsonValue } : {}) });
  }
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  return 0;
}

main().then((code) => { process.exitCode = code; }, (error: unknown) => {
  if (error instanceof IpcError) process.stderr.write(`rejected: ${error.code}\n`);
  else if (error instanceof DataPutError) process.stderr.write(`${error.message}; resume with --upload ${error.upload} --start-chunk ${error.chunk}\n`);
  else if (error instanceof TransportError) process.stderr.write(`${error.message}\n`);
  else process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
