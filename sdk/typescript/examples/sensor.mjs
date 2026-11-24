#!/usr/bin/env node
// Sensor component: read configuration, report health, keep readings in a
// durable stream and announce them on a topic, through a bounded buffer that
// absorbs daemon backpressure and short restarts.
//
// Environment: SUIFLEET_IPC_SOCKET, SUIFLEET_COMPONENT (the configured name
// owned by this process's Unix UID), SENSOR_STREAM, SENSOR_TOPIC, SENSOR_COUNT.
// The ACL needs config, health, publish (topic), stream_create/stream_append
// with a matching `streams` glob.
import { Client, IpcError, OutboundBuffer } from '@suifleet/component-ipc';

const client = new Client({
  socketPath: process.env.SUIFLEET_IPC_SOCKET ?? '/run/suifleet/ipc.sock',
  component: process.env.SUIFLEET_COMPONENT ?? 'sensor',
  timeoutMs: 30_000,
  connectAttempts: 20, // only failures before sending are retried
});
const stream = process.env.SENSOR_STREAM ?? 'sensor-readings';
const topic = process.env.SENSOR_TOPIC ?? 'sensor/events';
const count = Number(process.env.SENSOR_COUNT ?? 5);

const configuration = await client.configuration();
const interval = configuration.effective?.interval_ms ?? 0;
try {
  await client.streamCreate(stream, { maxSize: 65536, strategyOnFull: 'RejectNewData' });
} catch (error) {
  if (!(error instanceof IpcError && error.code === 'exists')) throw error;
}
await client.health('healthy');

const buffer = new OutboundBuffer(client, { maxItems: 64 });
for (let index = 0; index < count; index++) {
  const reading = { index, celsius: 20 + index / 10 };
  buffer.append(stream, reading);
  buffer.publish(topic, reading);
  if (interval) await new Promise((resolve) => setTimeout(resolve, interval));
}
const flushed = await buffer.flush({ maxAttempts: 10 });
if (flushed.error) {
  await client.health('degraded');
  console.error(`still pending: ${flushed.pending} (${flushed.error.message})`);
  process.exitCode = 1;
}
const description = await client.streamDescribe(stream);
console.log(JSON.stringify({ configuration: configuration.effective, sent: flushed.sent, pending: flushed.pending,
  stream: { name: description.name, oldest: description.oldest, newest: description.newest } }));
