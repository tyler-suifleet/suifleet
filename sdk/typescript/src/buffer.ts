/**
 * Bounded, in-memory, ordered outbound buffer for `publish` and `stream_append`.
 *
 * It exists for daemon backpressure (full inboxes, durable queue limits,
 * RejectNewData streams) and short daemon restarts. It is process memory: it
 * does not survive this process. Use a durable stream for data that must.
 *
 * Retry rules follow the daemon's outcome guarantees:
 * - publish IpcError: publication is all-or-nothing (inbox and durable spool
 *   backpressure store nothing), so resending cannot duplicate.
 * - stream_append IpcError with a stream protocol code: every coded refusal is
 *   raised before the record is stored. `full` and `unavailable` are retried;
 *   permanent codes (`invalid`, `retired`, `not-found`, ...) end the flush at
 *   once with the item still queued.
 * - stream_append IpcError `request_failed`: generic, and the daemon can return
 *   it after the record was stored (for example a failed segment cleanup), so
 *   it is treated like an `unknown` outcome below.
 * - TransportError `not_sent`: nothing reached the daemon; resending is safe.
 * - TransportError `unknown`: the message may already be delivered. The buffer
 *   stops, keeps the item at its head marked uncertain and throws
 *   UncertainDeliveryError. The caller decides with `resolveUncertain`.
 *
 * Payloads are snapshotted at enqueue: later changes to the caller's object do
 * not affect the queued message or the byte accounting.
 */
import type { Client } from './index.js';
import type { JsonValue } from './protocol.js';
import { IpcError, MAX_PUBLISH_PAYLOAD, TransportError } from './protocol.js';

/** A queued item. Its payload is a frozen, detached copy taken at enqueue. */
export type OutboundItem =
  | { readonly kind: 'publish'; readonly topic: string; readonly payload: JsonValue }
  | { readonly kind: 'stream'; readonly stream: string; readonly payload: JsonValue };

/** Stream refusals that store nothing and may succeed later. */
const RETRYABLE_STREAM_CODES = new Set(['full', 'unavailable']);

export interface OutboundBufferOptions {
  /** Maximum queued items. Default 256. */
  maxItems?: number;
  /** Maximum total serialized payload bytes. Default 1 MiB. */
  maxBytes?: number;
}

export interface FlushOptions {
  /** Attempts for the head item before flush returns with it still queued. Default 5. */
  maxAttempts?: number;
  /** First delay between attempts, doubled up to 5 s. Default 200. */
  retryDelayMs?: number;
  signal?: AbortSignal;
}

export interface FlushResult {
  /** Items delivered in this flush, in order. */
  sent: number;
  /** Items still queued. */
  pending: number;
  /** Last rejection of the head item when flush stopped early. */
  error?: IpcError | TransportError;
}

export class BufferFullError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BufferFullError';
  }
}

export class UncertainDeliveryError extends Error {
  readonly item: OutboundItem;
  /** TransportError `unknown`, or a stream_append `request_failed` IpcError. */
  constructor(item: OutboundItem, cause: TransportError | IpcError) {
    super(`delivery of the head item is uncertain: ${cause.message}`, { cause });
    this.name = 'UncertainDeliveryError';
    this.item = item;
  }
}

interface Entry { item: OutboundItem; bytes: number }

function deepFreeze(value: JsonValue): JsonValue {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) deepFreeze(child as JsonValue);
    Object.freeze(value);
  }
  return value;
}

/** Serialize once; queue a frozen copy that serializes to exactly these bytes. */
function snapshot(payload: JsonValue): { payload: JsonValue; bytes: number } {
  const text = JSON.stringify(payload ?? null);
  return { payload: deepFreeze(JSON.parse(text) as JsonValue), bytes: Buffer.byteLength(text) };
}

export class OutboundBuffer {
  readonly maxItems: number;
  readonly maxBytes: number;
  private readonly client: Client;
  private readonly entries: Entry[] = [];
  private bytes = 0;
  private uncertain = false;
  private flushing = false;

  constructor(client: Client, options: OutboundBufferOptions = {}) {
    this.client = client;
    this.maxItems = options.maxItems ?? 256;
    this.maxBytes = options.maxBytes ?? 1024 * 1024;
    if (!Number.isInteger(this.maxItems) || this.maxItems < 1) throw new RangeError('maxItems must be a positive integer');
    if (!Number.isInteger(this.maxBytes) || this.maxBytes < 1) throw new RangeError('maxBytes must be a positive integer');
  }

  get pending(): number { return this.entries.length; }
  get pendingBytes(): number { return this.bytes; }
  /** True after an `unknown` outcome until `resolveUncertain` is called. */
  get blocked(): boolean { return this.uncertain; }

  /** Queue a publication. Throws BufferFullError instead of growing past the bounds. */
  publish(topic: string, payload: JsonValue): void {
    const copy = snapshot(payload);
    if (copy.bytes > MAX_PUBLISH_PAYLOAD) throw new RangeError('publish payload exceeds 4096 serialized bytes');
    this.push({ item: { kind: 'publish', topic, payload: copy.payload }, bytes: copy.bytes });
  }

  /** Queue a durable stream append. */
  append(stream: string, payload: JsonValue): void {
    const copy = snapshot(payload);
    this.push({ item: { kind: 'stream', stream, payload: copy.payload }, bytes: copy.bytes });
  }

  private push(entry: Entry): void {
    if (this.entries.length >= this.maxItems) throw new BufferFullError(`outbound buffer holds ${this.maxItems} items`);
    if (this.bytes + entry.bytes > this.maxBytes) throw new BufferFullError(`outbound buffer holds ${this.maxBytes} bytes`);
    this.entries.push(entry);
    this.bytes += entry.bytes;
  }

  /**
   * After UncertainDeliveryError: `retry` resends the head (may duplicate it),
   * `discard` drops it (may lose it). Inspect the destination first when possible.
   */
  resolveUncertain(decision: 'retry' | 'discard'): void {
    if (!this.uncertain) throw new Error('no uncertain delivery');
    if (decision === 'discard') this.shift();
    this.uncertain = false;
  }

  private shift(): void {
    const entry = this.entries.shift();
    if (entry) this.bytes -= entry.bytes;
  }

  /** Send queued items in order. Never drops an item and never reorders. */
  async flush(options: FlushOptions = {}): Promise<FlushResult> {
    if (this.uncertain) throw new Error('outbound buffer is blocked by an uncertain delivery; call resolveUncertain');
    if (this.flushing) throw new Error('flush already in progress');
    this.flushing = true;
    const maxAttempts = options.maxAttempts ?? 5;
    let sent = 0;
    try {
      while (this.entries.length > 0) {
        const entry = this.entries[0] as Entry;
        let delay = options.retryDelayMs ?? 200;
        for (let attempt = 1; ; attempt++) {
          try {
            options.signal?.throwIfAborted();
            const request = options.signal ? { signal: options.signal } : {};
            if (entry.item.kind === 'publish') {
              await this.client.request('publish', { topic: entry.item.topic, payload: entry.item.payload, ...request });
            } else {
              await this.client.request('stream_append', { topic: entry.item.stream, payload: entry.item.payload, ...request });
            }
            this.shift();
            sent++;
            break;
          } catch (error) {
            if (error instanceof TransportError && error.outcome === 'unknown') {
              this.uncertain = true;
              throw new UncertainDeliveryError(entry.item, error);
            }
            if (!(error instanceof IpcError || error instanceof TransportError)) throw error;
            if (entry.item.kind === 'stream' && error instanceof IpcError) {
              // A generic failure may follow a stored append; never replay it blindly.
              if (error.code === 'request_failed') {
                this.uncertain = true;
                throw new UncertainDeliveryError(entry.item, error);
              }
              if (!RETRYABLE_STREAM_CODES.has(error.code)) return { sent, pending: this.entries.length, error };
            }
            if (attempt >= maxAttempts) return { sent, pending: this.entries.length, error };
            await new Promise<void>((resolve, reject) => {
              const signal = options.signal;
              const abort = () => { clearTimeout(timer); reject(signal?.reason); };
              const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, delay);
              signal?.addEventListener('abort', abort, { once: true });
            });
            delay = Math.min(delay * 2, 5000);
          }
        }
      }
      return { sent, pending: 0 };
    } finally {
      this.flushing = false;
    }
  }
}
