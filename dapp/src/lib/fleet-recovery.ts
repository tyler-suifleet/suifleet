// SF-091 recovery assessment over the SF-082 verified readers. It classifies
// what a restored operator can read and why; it never builds or submits a
// transaction, never falls back to an indexer and never treats a dependency
// outage as data loss (or a successful download as authentication).
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import type { WalrusClient } from '@mysten/walrus';
import { ObjectError } from '@mysten/sui/client';
import { bcs } from '@mysten/sui/bcs';
import { normalizeStructTag, normalizeSuiAddress as id, toHex } from '@mysten/sui/utils';
import { ExpiredSessionKeyError, GeneralError, InternalError, NoAccessError, SealError, TooManyFailedFetchKeyRequestsError } from '@mysten/seal';
import { fetchChainHealth, listFleetDeviceIds, readDeviceData, readFleetData, type FleetDataConfig } from './fleet-data';
import { ArchiveSealBindingError, BlobRecordBcs, fetchVerifiedArchive, readAnchors, type Anchor, type Archive } from './fleet-bulk';
import { readIdentity } from './telemetry';
import type { DeploymentEvent } from './sui';

export type Dependency = 'rpc' | 'walrus' | 'aggregator' | 'keyserver' | 'session';
export type ArchiveState = 'available' | 'outage' | 'expired' | 'released' | 'device_revoked'
  | 'key_epoch_rotated' | 'access_denied' | 'missing' | 'invalid';
/** What the operator does next. Only `retry` and `new_session` are resumable outages. */
export type Recovery = 'none' | 'retry' | 'new_session' | 'grant_access' | 'not_recoverable' | 'incident';
export interface ArchiveAssessment {
  index: number;
  state: ArchiveState;
  /** `ciphertext`: digest and Seal policy binding verified without requesting keys. */
  verified?: 'plaintext' | 'ciphertext';
  recovery: Recovery;
  dependency?: Dependency;
  reason: string;
  archive?: Archive;
}

const RECOVERY: Record<ArchiveState, Recovery> = {
  available: 'none', outage: 'retry', access_denied: 'grant_access',
  // Device spools are deleted after a verified anchor, and old-epoch or
  // revoked-device keys are refused by policy: no plaintext copy exists to restore.
  expired: 'not_recoverable', released: 'not_recoverable', key_epoch_rotated: 'not_recoverable', device_revoked: 'not_recoverable',
  missing: 'incident', invalid: 'incident',
};
const outcome = (index: number, state: ArchiveState, reason: string, dependency?: Dependency): ArchiveAssessment =>
  ({ index, state, recovery: dependency === 'session' ? 'new_session' : RECOVERY[state], reason, ...(dependency ? { dependency } : {}) });
const text = (error: unknown) => (error instanceof Error ? `${error.name}: ${error.message}` : String(error)).slice(0, 200);
const chainAnswer = (error: unknown) => error instanceof ObjectError && (error.reason === 'notFound' || error.reason === 'deleted');
const hasError = (error: unknown, type: abstract new (...args: never[]) => Error): boolean =>
  error instanceof type || (error instanceof Error && error.cause !== undefined && hasError(error.cause, type));
/** Only key-server unavailability is resumable by retrying the same decryptor.
 * Local provider/identity binding refusals and other Seal client or request
 * errors (invalid PTB, package, signature, ciphertext, threshold, inconsistent
 * servers) are incidents, never outages. Non-Seal errors are transport. */
const keyServerOutage = (error: unknown) => !hasError(error, ArchiveSealBindingError)
  && (!(error instanceof SealError) || [InternalError, GeneralError, TooManyFailedFetchKeyRequestsError].some(type => error instanceof type));

type Reads = Pick<SuiGrpcClient, 'getObject' | 'getDynamicField' | 'listDynamicFields' | 'ledgerService'>;
/** Record transport failures of the fullnode separately from its answers. A
 * missing object is an answer; a refused binding is our own verification. */
function observed<T extends Reads>(client: T, failures: unknown[]): T {
  // gRPC unary calls are thenables; awaiting yields the same finished call.
  const wrap = <F extends (...args: never[]) => PromiseLike<unknown>>(call: F) => (async (...args: Parameters<F>) => {
    try { return await call(...args); } catch (error) { if (!chainAnswer(error)) failures.push(error); throw error; }
  }) as unknown as F;
  const ledger = { ...client.ledgerService, getServiceInfo: wrap(client.ledgerService.getServiceInfo.bind(client.ledgerService)) };
  return new Proxy(client, { get(target, prop, receiver) {
    if (prop === 'ledgerService') return ledger;
    if (prop === 'getObject' || prop === 'getDynamicField' || prop === 'listDynamicFields') return wrap(target[prop].bind(target));
    return Reflect.get(target, prop, receiver);
  } });
}

/** Page the public chain index from a clean environment. Bounded; duplicate or
 * non-advancing pages fail closed rather than silently truncating the fleet. */
export async function discoverFleet(client: SuiGrpcClient, config: FleetDataConfig, maxPages = 100): Promise<string[]> {
  const devices = new Set<string>();
  let cursor: string | null = null;
  for (let pages = 0; ; pages++) {
    if (pages >= maxPages) throw Error('Fleet index exceeds the recovery page bound');
    const page = await listFleetDeviceIds(client, config, cursor);
    for (const device of page.devices) {
      if (devices.has(device)) throw Error('Duplicate fleet device across index pages');
      devices.add(device);
    }
    if (!page.hasNextPage) return [...devices];
    cursor = page.cursor;
  }
}

/** Live fleet-owned storage record, as `requireArchiveReadable` reads it. */
async function liveRecord(client: Reads, config: FleetDataConfig, anchor: Anchor) {
  const { object } = await client.getObject({ objectId: config.storageId, include: { content: true } });
  const [origin] = normalizeStructTag(object.type).split('::');
  try {
    const field = await client.getDynamicField({ parentId: config.storageId, name: {
      type: `${origin}::fleet_storage::RecordKey`, bcs: bcs.struct('RecordKey', { blob_object_id: bcs.Address }).serialize({ blob_object_id: anchor.blob_object_id }).toBytes(),
    } });
    return BlobRecordBcs.parse(field.dynamicField.value.bcs);
  } catch (error) {
    if (chainAnswer(error)) return undefined;
    throw error;
  }
}

/** Signals that every pre-key check passed; never escapes `assessArchive`. */
class CiphertextVerified extends Error {}

/**
 * Read one archive through the complete SF-082 verified path and explain a
 * refusal. Dependency failures are attributed by wrapping the fullnode, Walrus
 * committee read, aggregator fetch and key-server decryptor; only when every
 * dependency answered is the refusal attributed to chain state or tampering.
 * Without `decrypt`, availability is checked up to key release: chain policy,
 * live record, ciphertext digest and Seal namespace/epoch/AAD binding.
 */
export async function assessArchive(client: SuiGrpcClient, walrus: Pick<WalrusClient, 'systemState'>, config: FleetDataConfig,
  deviceId: string, index: number, aggregator: string, decrypt?: (encrypted: Uint8Array) => Promise<Uint8Array>,
  fetcher: typeof fetch = fetch): Promise<ArchiveAssessment> {
  const rpc: unknown[] = [];
  let walrusError: unknown, aggregatorError: unknown, keyError: unknown;
  const reads = observed(client, rpc);
  const committee = { async systemState() { try { return await walrus.systemState(); } catch (error) { walrusError = error; throw error; } } };
  const download: typeof fetch = async (input, init) => {
    let response: Response;
    try { response = await fetcher(input, init); } catch (error) { aggregatorError = error; throw error; }
    // A certified, unexpired blob that one aggregator cannot serve is still an
    // outage of that aggregator; chain records decide expiry below.
    if (!response.ok) { aggregatorError = `aggregator HTTP ${response.status}`; return response; }
    if (!response.body) return response;
    // Fetch resolves at the headers: a reset or timeout while the body streams
    // is still the aggregator's. Only read rejections are recorded; the size
    // bound and digest/binding checks are the reader's own refusals.
    const reader = response.body.getReader();
    const body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        let chunk: ReadableStreamReadResult<Uint8Array>;
        try { chunk = await reader.read(); } catch (error) { aggregatorError = error; throw error; }
        if (chunk.done) controller.close(); else controller.enqueue(chunk.value);
      },
      cancel(reason) { return reader.cancel(reason); },
    });
    return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
  };
  const keys = async (encrypted: Uint8Array) => {
    if (!decrypt) throw new CiphertextVerified();
    try { return await decrypt(encrypted); } catch (error) { keyError = error; throw error; }
  };
  let refusal: unknown;
  try {
    const archive = await fetchVerifiedArchive(reads, committee, config, deviceId, index, aggregator, keys, download);
    return { ...outcome(index, 'available', 'signed archive verified after decryption'), verified: 'plaintext', archive };
  } catch (error) { refusal = error; }
  if (refusal instanceof CiphertextVerified) {
    return { ...outcome(index, 'available', 'ciphertext digest and Seal policy binding verified; no keys requested'), verified: 'ciphertext' };
  }
  if (rpc.length) return outcome(index, 'outage', text(rpc[0]), 'rpc');
  if (walrusError) return outcome(index, 'outage', text(walrusError), 'walrus');
  if (keyError && hasError(keyError, NoAccessError)) return outcome(index, 'access_denied', text(keyError));
  if (keyError && hasError(keyError, ExpiredSessionKeyError)) return outcome(index, 'outage', text(keyError), 'session');
  try {
    const fleet = await readFleetData(reads, config);
    const [anchor] = await readAnchors(reads, config, deviceId, index, 1);
    if (!anchor) return outcome(index, 'missing', 'archive index does not exist');
    const identity = await readIdentity(reads, deviceId);
    if (!identity || identity.revoked) return outcome(index, 'device_revoked', 'current registry identity is missing or revoked');
    if (BigInt(anchor.key_epoch) !== BigInt(fleet.key_epoch)) return outcome(index, 'key_epoch_rotated', `anchor epoch ${anchor.key_epoch}, fleet epoch ${fleet.key_epoch}`);
    const record = await liveRecord(reads, config, anchor);
    if (!record) return outcome(index, 'released', 'fleet storage record was released');
    const { committee: { epoch } } = await committee.systemState();
    if (epoch >= record.end_epoch) return outcome(index, 'expired', `storage ended at epoch ${record.end_epoch}; current ${epoch}`);
  } catch (error) {
    if (rpc.length) return outcome(index, 'outage', text(rpc[0]), 'rpc');
    if (walrusError) return outcome(index, 'outage', text(walrusError), 'walrus');
    return outcome(index, 'invalid', text(error));
  }
  if (aggregatorError) return outcome(index, 'outage', text(aggregatorError), 'aggregator');
  if (keyError) return keyServerOutage(keyError) ? outcome(index, 'outage', text(keyError), 'keyserver') : outcome(index, 'invalid', text(keyError));
  return outcome(index, 'invalid', text(refusal));
}

export interface PublicDeviceRecord {
  deviceId: string;
  health: string;
  lastSeenMs?: number;
  healthSeq: string;
  batchSeq: string;
  anchors: { seq: string; kind: number; blobObjectId: string; blobId: string; ciphertextSha256: string; signer: string; atMs: string; endEpoch: number; keyEpoch: string }[];
}
export const EXPORT_DEVICE_LIMIT = 1024;
export const EXPORT_ANCHOR_LIMIT = 4096;

/** Customer exit export of public chain records only: bindings, health status
 * and archive references/digests. Ciphertext, plaintext, keys and sessions are
 * never included; authorized decrypted export is a separate owner action. */
export async function exportPublicRecords(client: SuiGrpcClient, config: FleetDataConfig, deviceIds: string[], now = Date.now()) {
  if (deviceIds.length > EXPORT_DEVICE_LIMIT) throw Error('Export exceeds the device bound; export in pages');
  const fleet = await readFleetData(client, config);
  const health = await fetchChainHealth(client, config, deviceIds, now);
  const devices: PublicDeviceRecord[] = [];
  for (const deviceId of deviceIds) {
    const writer = await readDeviceData(client, config, deviceId);
    if (!writer) throw Error('Indexed device has no writer');
    const count = Number(writer.anchor_count);
    if (!Number.isSafeInteger(count) || count > EXPORT_ANCHOR_LIMIT) throw Error('Device archive index exceeds the export bound');
    const anchors: Anchor[] = [];
    while (anchors.length < count) {
      const page = await readAnchors(client, config, deviceId, anchors.length, 64);
      if (!page.length) throw Error('Archive index ended before its count');
      anchors.push(...page);
    }
    const h = health[deviceId];
    devices.push({ deviceId: id(deviceId), health: h.status, ...(h.lastSeenMs !== undefined ? { lastSeenMs: h.lastSeenMs } : {}),
      healthSeq: writer.health_seq, batchSeq: writer.batch_seq, anchors: anchors.map(a => ({
        seq: a.seq, kind: a.kind, blobObjectId: id(a.blob_object_id), blobId: a.blob_id, ciphertextSha256: toHex(new Uint8Array(a.ciphertext_sha256)),
        signer: id(a.signer), atMs: a.at_ms, endEpoch: a.end_epoch, keyEpoch: a.key_epoch })) });
  }
  return { schema: 1, kind: 'suifleet-public-records', exportedAtMs: now, chainIdentifier: config.chainIdentifier,
    fleetId: config.fleetId, budgetId: config.budgetId, storageId: config.storageId, registryId: config.registryId,
    keyEpoch: fleet.key_epoch, support: fleet.support.map(a => id(a)), healthIntervalMs: fleet.health_interval_ms, devices };
}

/** A customer-retained deployment record (exported before the incident). */
export interface RetainedDeployment {
  deploymentId?: string;
  componentName: string;
  version: string;
  recipeBlobId: string;
  artifactSha256: string;
  targetDevices: string[];
  targetGroups: string[];
}
const targets = (values: string[]) => values.map(v => id(v)).sort().join();
const sameDeployment = (a: RetainedDeployment, b: DeploymentEvent) => a.componentName === b.componentName && a.version === b.version
  && a.recipeBlobId === b.recipeBlobId && a.artifactSha256.toLowerCase() === b.artifactSha256.toLowerCase()
  && targets(a.targetDevices) === targets(b.targetDevices) && targets(a.targetGroups) === targets(b.targetGroups);

/** Resume after restore without duplicating deployments: everything already on
 * chain is kept (by ID, or by identical content and targets); nothing is
 * created by this function. Only verified creators are considered existing. */
export function resumeDeployments(retained: RetainedDeployment[], existing: DeploymentEvent[], trustedCreators: string[]) {
  const creators = new Set(trustedCreators.map(c => id(c)));
  const onChain = existing.filter(e => creators.has(id(e.creator)));
  const present: { retained: RetainedDeployment; deploymentId: string }[] = [], missing: RetainedDeployment[] = [];
  for (const record of retained) {
    const match = onChain.find(e => (record.deploymentId && id(record.deploymentId) === id(e.deploymentId)) || sameDeployment(record, e));
    if (match) present.push({ retained: record, deploymentId: match.deploymentId });
    else missing.push(record);
  }
  const unknown = onChain.filter(e => !present.some(p => id(p.deploymentId) === id(e.deploymentId)));
  return { present, missing, unknown };
}
