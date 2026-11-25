/**
 * SuiFleet native component IPC v1 client for Node.js.
 *
 * This speaks the daemon's own Unix-stream protocol (docs/COMPONENT_IPC.md).
 * It is not AWS Greengrass IPC wire, SDK or CLI compatible. Identity is the
 * kernel peer UID of the calling process: nothing in a request (component name,
 * token, environment) authenticates it.
 */
import { createHash, randomBytes } from 'node:crypto';
import { createConnection } from 'node:net';
import { performance } from 'node:perf_hooks';
import {
  DATA_CHUNK_SIZE, DEFAULT_SOCKET, DataPutError, MAX_DATA_SIZE, MAX_FRAME, MAX_PUBLISH_PAYLOAD, MAX_TIMER_DELAY_MS, ProtocolError, TransportError,
  decodeResponse, encodeRequest, validateDataMetadata,
} from './protocol.js';
import type { DataMetadata, DataPutReceipt, JsonObject, JsonValue } from './protocol.js';

export * from './protocol.js';

export interface ClientOptions {
  /** Absolute socket path; default /run/suifleet/ipc.sock. */
  socketPath?: string;
  /** Configured component name owned by this process's UID ('' for administrators). */
  component?: string;
  /**
   * Whole-request deadline, started when `request` is called. It covers every
   * connection attempt and the backoff between them, so a request is never
   * first sent after it expires. The daemon ends connections after 120 s.
   * A finite number in (0, 2147483647] (Node's maximum timer delay); anything
   * else throws RangeError before connecting. Default 120000.
   */
  timeoutMs?: number;
  /**
   * Connection attempts per request (>= 1, default 1). Only failures before any
   * byte was sent (ENOENT, ECONNREFUSED, EAGAIN) are retried, so a retry never
   * duplicates a request, for example while the daemon restarts.
   */
  connectAttempts?: number;
  /** First delay between connection attempts (finite, 0..2147483647), doubled up to 5 s. Default 250. */
  connectRetryDelayMs?: number;
}

export interface RequestOptions {
  component?: string;
  topic?: string;
  payload?: JsonValue;
  signal?: AbortSignal;
}

export type HealthState = 'healthy' | 'degraded' | 'broken';

export interface ComponentConfiguration {
  version?: string;
  deployment?: string;
  effective?: JsonValue;
  defaults?: JsonValue;
  [field: string]: JsonValue | undefined;
}

export interface PublishResult { delivered: number; durable: number; volatile: number }

/** Message from a bounded, volatile SF-027 inbox (no id, no ack). */
export interface InboxMessage { sender: string; payload: JsonValue }
/** Message from a durable SF-032 subscription. QoS 1 messages wait for `ack`. */
export interface DurableMessage { id: number; topic: string; sender: string; payload: JsonValue; published_ms: number }
export type ReceivedMessage = InboxMessage | DurableMessage;
export function isDurable(message: ReceivedMessage): message is DurableMessage {
  return typeof (message as DurableMessage).id === 'number';
}

export type StrategyOnFull = 'RejectNewData' | 'OverwriteOldestData';
export interface StreamDefinition {
  maxSize: number;
  strategyOnFull?: StrategyOnFull;
  persistence?: 'File' | 'Memory';
  timeToLiveSeconds?: number;
}
export interface StreamMessage { sequence: number; appended_ms: number; payload?: JsonValue; data_base64?: string }
export interface StreamDescription {
  name: string; max_size: number; strategy_on_full: StrategyOnFull; persistence: string;
  time_to_live_seconds: number | null; oldest: number; newest: number; bytes: number; messages: number;
  [field: string]: JsonValue | undefined;
}

export interface ShadowState { desired?: JsonValue; reported?: JsonValue; delta?: JsonValue }
export interface ShadowDocument {
  state: ShadowState;
  version: number;
  clientToken?: string;
  [field: string]: JsonValue | ShadowState | undefined;
}

export interface DataPutOptions {
  contentType?: string;
  /**
   * Public string-to-string metadata, checked locally against the daemon's
   * limits (see validateDataMetadata); never put secrets here.
   */
  metadata?: DataMetadata;
  /** Stable 64-hex upload ID; preserve it across an uncertain retry. */
  upload?: string;
  startChunk?: number;
  signal?: AbortSignal;
}
export interface DataPutResult { upload: string; start_chunk: number; chunks: DataPutReceipt[] }

const RETRYABLE_CONNECT = new Set(['ENOENT', 'ECONNREFUSED', 'EAGAIN']);
const sleep = (ms: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  if (signal?.aborted) return reject(signal.reason);
  const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, ms);
  const abort = () => { clearTimeout(timer); reject(signal?.reason); };
  signal?.addEventListener('abort', abort, { once: true });
});

export class Client {
  readonly socketPath: string;
  readonly component: string;
  readonly timeoutMs: number;
  readonly connectAttempts: number;
  readonly connectRetryDelayMs: number;

  constructor(options: ClientOptions = {}) {
    this.socketPath = options.socketPath ?? DEFAULT_SOCKET;
    this.component = options.component ?? '';
    this.timeoutMs = options.timeoutMs ?? 120_000;
    this.connectAttempts = options.connectAttempts ?? 1;
    this.connectRetryDelayMs = options.connectRetryDelayMs ?? 250;
    if (!Number.isInteger(this.connectAttempts) || this.connectAttempts < 1) throw new RangeError('connectAttempts must be a positive integer');
    if (typeof this.timeoutMs !== 'number' || !Number.isFinite(this.timeoutMs) || this.timeoutMs <= 0 || this.timeoutMs > MAX_TIMER_DELAY_MS) {
      throw new RangeError(`timeoutMs must be a finite number in (0, ${MAX_TIMER_DELAY_MS}]`);
    }
    if (typeof this.connectRetryDelayMs !== 'number' || !Number.isFinite(this.connectRetryDelayMs) || this.connectRetryDelayMs < 0
      || this.connectRetryDelayMs > MAX_TIMER_DELAY_MS) {
      throw new RangeError(`connectRetryDelayMs must be a finite number in [0, ${MAX_TIMER_DELAY_MS}]`);
    }
  }

  /**
   * Send one request on a fresh connection (the protocol has no pipelining) and
   * return the response `data`. The SDK never retries after a byte was sent.
   */
  async request<T = unknown>(operation: string, options: RequestOptions = {}): Promise<T> {
    const frame = encodeRequest(operation, options.component ?? this.component, options.topic ?? '', options.payload);
    const deadline = performance.now() + this.timeoutMs;
    let delay = this.connectRetryDelayMs;
    for (let attempt = 1; ; attempt++) {
      try {
        return decodeResponse(operation, await this.exchange(operation, frame, deadline, options.signal)) as T;
      } catch (error) {
        const retry = error instanceof TransportError && error.outcome === 'not_sent'
          && error.errno !== undefined && RETRYABLE_CONNECT.has(error.errno) && attempt < this.connectAttempts;
        if (!retry) throw error;
        // Backoff never extends past the request deadline; the next exchange
        // then fails as not_sent instead of dispatching late.
        try {
          await sleep(Math.max(0, Math.min(delay, deadline - performance.now())), options.signal);
        } catch (reason) {
          // Cancelled during backoff: nothing was sent, so keep the documented classification.
          throw new TransportError(operation, 'not_sent', 'aborted', { cause: reason });
        }
        delay = Math.min(delay * 2, 5000);
      }
    }
  }

  private exchange(operation: string, frame: Buffer, deadline: number, signal?: AbortSignal): Promise<Buffer> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new TransportError(operation, 'not_sent', 'aborted', { cause: signal.reason }));
        return;
      }
      const remaining = deadline - performance.now();
      if (remaining <= 0) {
        reject(new TransportError(operation, 'not_sent', `timed out after ${this.timeoutMs} ms`));
        return;
      }
      let sent = false;
      let settled = false;
      const chunks: Buffer[] = [];
      let length = 0;
      const socket = createConnection({ path: this.socketPath });
      const finish = (error: Error | null, line?: Buffer) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
        socket.destroy();
        if (error) reject(error); else resolve(line as Buffer);
      };
      const timer = setTimeout(() => finish(new TransportError(operation, sent ? 'unknown' : 'not_sent', `timed out after ${this.timeoutMs} ms`)), remaining);
      const abort = () => finish(new TransportError(operation, sent ? 'unknown' : 'not_sent', 'aborted', { cause: signal?.reason }));
      signal?.addEventListener('abort', abort, { once: true });
      socket.once('connect', () => {
        if (settled) return;
        // The connect callback can run after an event-loop stall past the
        // deadline while the timer is still queued: never dispatch late.
        if (performance.now() >= deadline) {
          finish(new TransportError(operation, 'not_sent', `timed out after ${this.timeoutMs} ms`));
          return;
        }
        sent = true;
        socket.write(frame);
      });
      socket.on('data', (chunk: Buffer) => {
        const newline = chunk.indexOf(0x0a);
        if (newline >= 0) {
          if (length + newline + 1 > MAX_FRAME) return finish(new ProtocolError(operation, 'response exceeds IPC frame limit'));
          chunks.push(chunk.subarray(0, newline));
          return finish(null, Buffer.concat(chunks));
        }
        length += chunk.length;
        if (length >= MAX_FRAME) return finish(new ProtocolError(operation, 'response exceeds IPC frame limit'));
        chunks.push(chunk);
      });
      socket.on('end', () => finish(new TransportError(operation, sent ? 'unknown' : 'not_sent', 'connection closed before a complete response')));
      socket.on('close', () => finish(new TransportError(operation, sent ? 'unknown' : 'not_sent', 'connection closed before a complete response')));
      socket.on('error', (error: NodeJS.ErrnoException) => finish(new TransportError(operation, sent ? 'unknown' : 'not_sent',
        error.code ?? error.message, { cause: error, errno: error.code })));
    });
  }

  // Local component management. install/list are administrator only.
  install(deploymentId: string): Promise<JsonObject> { return this.request('install', { payload: deploymentId }); }
  list(): Promise<JsonObject> { return this.request('list'); }
  start(): Promise<JsonValue> { return this.request('start'); }
  stop(): Promise<JsonValue> { return this.request('stop'); }
  restart(): Promise<JsonValue> { return this.request('restart'); }
  status(): Promise<JsonObject> { return this.request('status'); }
  async logs(): Promise<string> { return (await this.request<{ tail: string }>('logs')).tail; }

  /**
   * Public committed configuration; confidential overlay values are never returned. Poll to observe changes.
   * `null` when the component has no committed configuration yet (for example before its first install).
   */
  async configuration(): Promise<ComponentConfiguration | null> {
    return (await this.request<{ configuration: ComponentConfiguration | null }>('config')).configuration ?? null;
  }

  /** Volatile self-reported health. It never asserts firmware or boot health. */
  health(state: HealthState): Promise<{ reported: boolean }> { return this.request('health', { payload: state }); }

  // Messaging (SF-027 volatile inboxes and SF-032 durable subscriptions).
  async publish(topic: string, payload: JsonValue): Promise<PublishResult> {
    if (Buffer.byteLength(JSON.stringify(payload ?? null)) > MAX_PUBLISH_PAYLOAD) throw new RangeError('publish payload exceeds 4096 serialized bytes');
    return this.request('publish', { topic, payload });
  }
  /**
   * Nonblocking. Volatile inbox and QoS 0 reads are destructive; a QoS 1 read
   * returns the oldest unacknowledged message again until `ack`.
   */
  receive(topic: string): Promise<ReceivedMessage | null> { return this.request('receive', { topic }); }
  subscribe(filter: string, qos: 0 | 1 = 1): Promise<{ subscribed: string; qos: number }> {
    return this.request('subscribe', { topic: filter, payload: { qos } });
  }
  unsubscribe(filter: string): Promise<{ unsubscribed: boolean }> { return this.request('unsubscribe', { topic: filter }); }
  ack(filter: string, messageId: number): Promise<{ acknowledged: boolean }> {
    return this.request('ack', { topic: filter, payload: messageId });
  }
  /** Administrator only: subscription and bridge counters. */
  messaging(): Promise<JsonObject> { return this.request('messaging'); }
  /** Administrator only (SF-035): action list, enroll, revoke or remove. */
  clientDevices(action: 'list' | 'enroll' | 'revoke' | 'remove', fields: JsonObject = {}): Promise<JsonValue> {
    return this.request('client_devices', { payload: { ...fields, action } });
  }

  /**
   * Durable subscription iterator. For QoS 1, call `ack` (or `delivery.ack()`)
   * before pulling the next value; otherwise the same message is delivered
   * again (at-least-once). Polls `receive` while the queue is empty. Failures
   * that sent no byte (daemon restarting) are retried until `signal` aborts;
   * any other failure ends the iteration with that error. Aborting while idle
   * or before a request is sent ends the iteration normally; aborting a
   * `receive` already sent throws its `TransportError` (outcome `unknown`),
   * because the daemon may have consumed a message.
   */
  async *messages(filter: string, options: { pollIntervalMs?: number; maxReconnectDelayMs?: number; signal?: AbortSignal } = {}):
    AsyncGenerator<{ message: ReceivedMessage; ack: () => Promise<boolean> }, void, undefined> {
    const interval = options.pollIntervalMs ?? 500;
    const maxDelay = options.maxReconnectDelayMs ?? 5000;
    let delay = interval;
    const signal = options.signal;
    while (!signal?.aborted) {
      let message: ReceivedMessage | null;
      try {
        message = await this.request<ReceivedMessage | null>('receive', signal === undefined ? { topic: filter } : { topic: filter, signal });
        delay = interval;
      } catch (error) {
        // A receive that was already sent may have been consumed by the daemon
        // (volatile and QoS 0 reads are destructive), so cancelling it must not
        // look like a clean stop. Abort before sending or while idle ends cleanly.
        if (signal?.aborted && !(error instanceof TransportError && error.outcome === 'unknown')) return;
        if (error instanceof TransportError && error.outcome === 'not_sent') {
          try { await sleep(delay, signal); } catch { return; }
          delay = Math.min(delay * 2, maxDelay);
          continue;
        }
        throw error;
      }
      if (message === null) {
        try { await sleep(interval, signal); } catch { return; }
        continue;
      }
      const received = message;
      yield {
        message: received,
        ack: async () => isDurable(received) ? (await this.ack(filter, received.id)).acknowledged : true,
      };
    }
  }

  // Durable streams (SF-034).
  streamCreate(name: string, definition: StreamDefinition): Promise<{ created: string }> {
    const payload: JsonObject = { max_size: definition.maxSize, strategy_on_full: definition.strategyOnFull ?? 'RejectNewData',
      persistence: definition.persistence ?? 'File' };
    if (definition.timeToLiveSeconds !== undefined) payload.time_to_live_seconds = definition.timeToLiveSeconds;
    return this.request('stream_create', { topic: name, payload });
  }
  async streamAppend(name: string, value: JsonValue): Promise<number> {
    return (await this.request<{ sequence: number }>('stream_append', { topic: name, payload: value })).sequence;
  }
  /** Nonblocking; fewer than `minMessages` available fails with code `not-enough-messages`. */
  async streamRead(name: string, options: { start?: number; minMessages?: number; maxMessages?: number } = {}): Promise<StreamMessage[]> {
    return (await this.request<{ messages: StreamMessage[] }>('stream_read', { topic: name,
      payload: { start: options.start ?? 0, min: options.minMessages ?? 1, max: options.maxMessages ?? 1 } })).messages;
  }
  streamDescribe(name: string): Promise<StreamDescription> { return this.request('stream_describe', { topic: name }); }
  streamDelete(name: string): Promise<{ deleted: string }> { return this.request('stream_delete', { topic: name }); }
  async streamList(): Promise<string[]> { return (await this.request<{ streams: string[] }>('stream_list')).streams; }
  /** Trusted operator-declared external exporter only. */
  streamExportAck(name: string, sequence: number): Promise<{ exported: number }> {
    return this.request('stream_export_ack', { topic: name, payload: { sequence } });
  }
  /** Trusted operator-declared external exporter only: numeric counters, never secrets. */
  streamExportStatus(name: string, status: { batches: number; failures: number; consecutiveFailures: number; lastSuccessMs?: number | null }):
    Promise<{ updated: boolean }> {
    return this.request('stream_export_status', { topic: name, payload: { batches: status.batches, failures: status.failures,
      consecutive_failures: status.consecutiveFailures, last_success_ms: status.lastSuccessMs ?? null } });
  }

  // Device shadows (SF-033). '' is the classic shadow.
  shadowGet(name = ''): Promise<ShadowDocument> { return this.request('shadow_get', { topic: name }); }
  /** `null` values delete keys. `version` makes the update conditional (code `conflict`). */
  shadowUpdate(name: string, state: { desired?: JsonValue; reported?: JsonValue }, options: { version?: number; clientToken?: string } = {}):
    Promise<ShadowDocument> {
    const payload: JsonObject = { state: state as JsonObject };
    if (options.version !== undefined) payload.version = options.version;
    if (options.clientToken !== undefined) payload.clientToken = options.clientToken;
    return this.request('shadow_update', { topic: name, payload });
  }
  shadowDelete(name = '', version?: number): Promise<JsonValue> {
    return this.request('shadow_delete', { topic: name, payload: version === undefined ? null : { version } });
  }
  async shadowList(): Promise<string[]> { return (await this.request<{ shadows: string[] }>('shadow_list')).shadows; }
  /** Administrator only: versions, sync status and notification counters. */
  shadows(): Promise<JsonObject> { return this.request('shadows'); }

  /**
   * Queue raw bytes for opaque storage in bounded chunks (SF-139). Returns
   * custody receipts, never a remote storage acknowledgement. On failure the
   * DataPutError carries the upload ID and chunk to resume from.
   */
  async putData(stream: string, key: string, data: Uint8Array, options: DataPutOptions = {}): Promise<DataPutResult> {
    if (data.length > MAX_DATA_SIZE) throw new RangeError('data exceeds 64 MiB');
    const upload = options.upload ?? randomBytes(32).toString('hex');
    if (!/^[0-9a-f]{64}$/.test(upload)) throw new RangeError('upload must be 64 lowercase hex characters');
    const chunks = Math.max(1, Math.ceil(data.length / DATA_CHUNK_SIZE));
    const startChunk = options.startChunk ?? 0;
    if (!Number.isInteger(startChunk) || startChunk < 0 || startChunk >= chunks) throw new RangeError('invalid startChunk');
    const metadata = validateDataMetadata(options.metadata === undefined ? {} : options.metadata);
    const receipts: DataPutReceipt[] = [];
    for (let chunk = startChunk; chunk < chunks; chunk++) {
      const part = Buffer.from(data.buffer, data.byteOffset + chunk * DATA_CHUNK_SIZE,
        Math.min(DATA_CHUNK_SIZE, data.length - chunk * DATA_CHUNK_SIZE));
      const descriptor: JsonObject = {
        stream, key, content_type: options.contentType ?? 'application/octet-stream', metadata,
        upload, chunk, chunks, total_size: data.length, size: part.length,
        sha256: [...createHash('sha256').update(part).digest()],
      };
      try {
        receipts.push(await this.request<DataPutReceipt>('data_put', { topic: stream,
          payload: { descriptor, data_base64: part.toString('base64') }, ...(options.signal ? { signal: options.signal } : {}) }));
      } catch (error) {
        throw new DataPutError(upload, chunk, receipts, error);
      }
    }
    return { upload, start_chunk: startChunk, chunks: receipts };
  }
}

export { OutboundBuffer, BufferFullError, UncertainDeliveryError } from './buffer.js';
export type { OutboundItem, OutboundBufferOptions, FlushOptions, FlushResult } from './buffer.js';
