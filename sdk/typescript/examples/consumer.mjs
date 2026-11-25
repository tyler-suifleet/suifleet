#!/usr/bin/env node
// Durable QoS 1 consumer: subscribe, process each message and acknowledge it.
// Unacknowledged messages survive daemon restarts and are delivered again.
//
// Environment: SUIFLEET_IPC_SOCKET, SUIFLEET_COMPONENT, CONSUMER_FILTER,
// CONSUMER_COUNT (stop after this many messages), CONSUMER_TIMEOUT_MS.
// The ACL needs subscribe/receive/ack and a SubscribeToTopic pubsub policy for
// exactly this filter.
import { Client } from '@suifleet/component-ipc';

const client = new Client({
  socketPath: process.env.SUIFLEET_IPC_SOCKET ?? '/run/suifleet/ipc.sock',
  component: process.env.SUIFLEET_COMPONENT ?? 'consumer',
  timeoutMs: 30_000,
  connectAttempts: 20,
});
const filter = process.env.CONSUMER_FILTER ?? 'sensor/#';
const count = Number(process.env.CONSUMER_COUNT ?? 1);
const signal = AbortSignal.timeout(Number(process.env.CONSUMER_TIMEOUT_MS ?? 60_000));

await client.subscribe(filter, 1);
const received = [];
for await (const delivery of client.messages(filter, { pollIntervalMs: 200, signal })) {
  received.push(delivery.message);
  await delivery.ack(); // acknowledge only after processing
  if (received.length >= count) break;
}
console.log(JSON.stringify({ received }));
if (received.length < count) process.exitCode = 1;
