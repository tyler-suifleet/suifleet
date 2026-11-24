/** Protocol v1 constants, errors and frame codec shared by the client and buffer. */
export const SDK_VERSION = '1.0.0';
/** Wire protocol version. Changing it requires a new SDK major version. */
export const PROTOCOL_VERSION = 1 as const;
/** Request and response frame limit, including the terminating LF. */
export const MAX_FRAME = 65536;
export const DEFAULT_SOCKET = '/run/suifleet/ipc.sock';
/** Serialized payload limit of `publish`. */
export const MAX_PUBLISH_PAYLOAD = 4096;
/** Raw bytes per `data_put` chunk, as in the Python SDK. */
export const DATA_CHUNK_SIZE = 24576;
export const MAX_DATA_SIZE = 64 * 1024 * 1024;

/** Operation names of protocol v1, in daemon order (interfaces/ipc-v1.json). */
export const OPERATIONS = [
  'list', 'status', 'start', 'stop', 'restart', 'logs', 'config', 'health', 'publish', 'receive', 'install',
  'subscribe', 'unsubscribe', 'ack', 'messaging', 'client_devices',
  'data_put', 'stream_create', 'stream_delete', 'stream_append', 'stream_read', 'stream_describe', 'stream_list',
  'stream_export_ack', 'stream_export_status',
  'shadow_get', 'shadow_update', 'shadow_delete', 'shadow_list', 'shadows',
] as const;
export type Operation = (typeof OPERATIONS)[number];
/** Operations a component ACL can never grant. */
export const ADMINISTRATOR_OPERATIONS = ['install', 'list', 'messaging', 'shadows', 'client_devices'] as const;

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

/**
 * The daemon processed the request and rejected it (`ok: false`).
 *
 * `code` is `request_failed` for authorization, validation and lifecycle
 * failures (details stay in daemon logs), or a protocol code for streams and
 * shadows such as `full`, `evicted`, `not-found`, `conflict` or `quota`.
 * Denied, invalid and future-version requests have no handler side effects.
 */
export class IpcError extends Error {
  readonly code: string;
  readonly operation: string;
  /** Oldest retained sequence, present with stream code `evicted`. */
  readonly oldest: number | undefined;
  constructor(operation: string, code: string, oldest?: number) {
    super(`${operation}: ${code}`);
    this.name = 'IpcError';
    this.operation = operation;
    this.code = code;
    this.oldest = oldest;
  }
}

/**
 * `not_sent`: no request byte reached the daemon (for example the socket is
 * missing during a daemon restart). Retrying is safe for every operation.
 *
 * `unknown`: the request may have been read and may still execute or have
 * executed (timeout, truncated or malformed response, closed connection).
 * Never retry a mutation blindly; inspect status, stream sequences, shadow
 * versions or deployment outcome first.
 */
export type Outcome = 'not_sent' | 'unknown';

export class TransportError extends Error {
  readonly outcome: Outcome;
  readonly operation: string;
  /** Node system error code such as ENOENT or ECONNREFUSED, when known. */
  readonly errno: string | undefined;
  constructor(operation: string, outcome: Outcome, message: string, options?: { cause?: unknown; errno?: string | undefined }) {
    super(`${operation}: ${message} (outcome ${outcome})`, options?.cause === undefined ? undefined : { cause: options.cause });
    this.name = 'TransportError';
    this.operation = operation;
    this.outcome = outcome;
    this.errno = options?.errno;
  }
}

/** The daemon answered with a frame this client cannot accept. Outcome is unknown. */
export class ProtocolError extends TransportError {
  constructor(operation: string, message: string) {
    super(operation, 'unknown', message);
    this.name = 'ProtocolError';
  }
}

export interface DataPutReceipt { sequence: number; state: string; storage_acknowledged: boolean; [field: string]: JsonValue }

/** Preserves the upload identity and the failed chunk for a resumed retry. */
export class DataPutError extends Error {
  readonly upload: string;
  readonly chunk: number;
  /** Receipts of chunks accepted before the failure. */
  readonly receipts: DataPutReceipt[];
  constructor(upload: string, chunk: number, receipts: DataPutReceipt[], cause: unknown) {
    super(`upload ${upload} chunk ${chunk}: ${cause instanceof Error ? cause.message : String(cause)}`, { cause });
    this.name = 'DataPutError';
    this.upload = upload;
    this.chunk = chunk;
    this.receipts = receipts;
  }
}

/** Encode one request frame exactly as the daemon expects it. */
export function encodeRequest(operation: string, component: string, topic: string, payload: JsonValue | undefined): Buffer {
  const frame = Buffer.from(JSON.stringify({
    version: PROTOCOL_VERSION, operation, component, topic, payload: payload === undefined ? null : payload,
  }) + '\n', 'utf8');
  if (frame.length > MAX_FRAME) throw new RangeError('request exceeds IPC frame limit');
  return frame;
}

/** Validate one response line and return `data` or throw IpcError/ProtocolError. */
export function decodeResponse(operation: string, line: Buffer): unknown {
  let value: unknown;
  try {
    value = JSON.parse(line.toString('utf8'));
  } catch {
    throw new ProtocolError(operation, 'malformed response');
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ProtocolError(operation, 'malformed response');
  const response = value as Record<string, unknown>;
  if (response.version !== PROTOCOL_VERSION) throw new ProtocolError(operation, 'unsupported response version');
  if (response.ok === true) {
    if (!('data' in response)) throw new ProtocolError(operation, 'response without data');
    return response.data;
  }
  if (response.ok === false) {
    const code = typeof response.error === 'string' ? response.error : 'request_failed';
    throw new IpcError(operation, code, typeof response.oldest === 'number' ? response.oldest : undefined);
  }
  throw new ProtocolError(operation, 'malformed response');
}
