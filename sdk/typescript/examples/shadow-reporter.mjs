#!/usr/bin/env node
// Follow a named shadow's desired state: apply each delta, then report it.
// The component writes only `reported`; desired comes from operators or the
// cloud. Delta notifications arrive on the durable delta topic.
//
// Environment: SUIFLEET_IPC_SOCKET, SUIFLEET_COMPONENT, SHADOW_THING,
// SHADOW_NAME, SHADOW_CHANGES (stop after this many deltas), SHADOW_TIMEOUT_MS.
// The ACL needs shadow_get/shadow_update (sections = ["reported"]) and
// subscribe/receive/ack with a SubscribeToTopic policy for the delta topic.
import { Client, IpcError } from '@suifleet/component-ipc';

const client = new Client({
  socketPath: process.env.SUIFLEET_IPC_SOCKET ?? '/run/suifleet/ipc.sock',
  component: process.env.SUIFLEET_COMPONENT ?? 'app',
  timeoutMs: 30_000,
  connectAttempts: 20,
});
const thing = process.env.SHADOW_THING ?? 'device1';
const name = process.env.SHADOW_NAME ?? 'config';
const changes = Number(process.env.SHADOW_CHANGES ?? 1);
const signal = AbortSignal.timeout(Number(process.env.SHADOW_TIMEOUT_MS ?? 60_000));
const deltaTopic = `$aws/things/${thing}/shadow/name/${name}/update/delta`;

await client.subscribe(deltaTopic, 1);
const applied = [];
for await (const delivery of client.messages(deltaTopic, { pollIntervalMs: 200, signal })) {
  // Notifications can be truncated or stale: always read the current document.
  const document = await client.shadowGet(name);
  const delta = document.state.delta;
  if (delta && typeof delta === 'object' && !Array.isArray(delta)) {
    try {
      // Conditional on the version we read; a concurrent change is a conflict.
      const result = await client.shadowUpdate(name, { reported: delta }, { version: document.version });
      applied.push({ delta, version: result.version });
    } catch (error) {
      if (!(error instanceof IpcError && error.code === 'conflict')) throw error;
      continue; // leave the notification unacknowledged and retry with a fresh read
    }
  }
  await delivery.ack();
  if (applied.length >= changes) break;
}
console.log(JSON.stringify({ applied }));
if (applied.length < changes) process.exitCode = 1;
