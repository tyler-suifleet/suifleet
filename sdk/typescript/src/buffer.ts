/**
 * Bounded, in-memory, ordered outbound buffer for `publish` and `stream_append`.
 *
 * It exists for daemon backpressure (full inboxes, durable queue limits,
 * RejectNewData streams) and short daemon restarts. It is process memory: it
 * does not survive this process. Use a durable stream for data that must.
 *
 * Retry rules follow the protocol's outcome guarantees:
 * - IpcError (the daemon rejected the request): publish backpressure is atomic
 *   and rejected appends store nothing, so resending cannot duplicate.
 * - TransportError `not_sent`: nothing reached the daemon; resending is safe.
 * - TransportError `unknown`: the message may already be delivered. The buffer
 *   stops, keeps the item at its head marked uncertain and throws
 *   UncertainDeliveryError. The caller decides with `resolveUncertain`.
 */
import type { Client } from './index.js';
import type { JsonValue } from './protocol.js';
import { IpcError, MAX_PUBLISH_PAYLOAD, TransportError } from './protocol.js';

export type OutboundItem =
  | { kind: 'publish'; topic: string; payload: JsonValue }
  | { kind: 'stream'; stream: string; payload: JsonValue };

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
  constructor(item: OutboundItem, cause: TransportError) {
    super(`delivery of the head item is uncertain: ${cause.message}`, { cause });
    this.name = 'UncertainDeliveryError';
    this.item = item;
  }
}

interface Entry { item: OutboundItem; bytes: number }

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
    const bytes = Buffer.byteLength(JSON.stringify(payload ?? null));
    if (bytes > MAX_PUBLISH_PAYLOAD) throw new RangeError('publish payload exceeds 4096 serialized bytes');
    this.push({ item: { kind: 'publish', topic, payload }, bytes });
  }

  /** Queue a durable stream append. */
  append(stream: string, payload: JsonValue): void {
    this.push({ item: { kind: 'stream', stream, payload }, bytes: Buffer.byteLength(JSON.stringify(payload ?? null)) });
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
