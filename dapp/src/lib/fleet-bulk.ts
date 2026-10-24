// Fleet archives authenticate the encrypted bytes on chain and the exact
// plaintext with the writer's personal-message signature. Never trust an
// aggregator, an optional relay, or a successful decryption as authentication.
import type { WalrusClient } from '@mysten/walrus';
import { bcs } from '@mysten/sui/bcs';
import { ObjectError } from '@mysten/sui/client';
import { isValidPersonalMessageSignature } from '@mysten/sui/verify';
import { fromBase64, normalizeSuiAddress as id, normalizeStructTag, toHex } from '@mysten/sui/utils';
import { EncryptedObject, ExpiredSessionKeyError, SealClient, type SessionKey } from '@mysten/seal';
import { Transaction } from '@mysten/sui/transactions';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { AnchorBcs, readDeviceData, readFleetData, type FleetDataConfig } from './fleet-data';
import { readIdentity } from './telemetry';
import { FleetStorageBcs } from './fleet-budget';
import { MAGIC, isEnvelope, type SealSettings } from './seal';
import { parseShadowSnapshot, type ShadowSnapshot } from './shadow-documents';

export const BlobRecordBcs = bcs.struct('BlobRecord', { device_id: bcs.option(bcs.Address), blob_id: bcs.u256(), size: bcs.u64(), encoded_size: bcs.u64(), end_epoch: bcs.u32(), uploaded_at_ms: bcs.u64() });

export const MAX_ARCHIVE_BYTES = 4 * 1024 * 1024;
export const MAX_ARCHIVE_RECORDS = 4096;
export interface ArchiveRecord { source: string; line: string }
export interface Archive {
  /** 2 (SF-084): also carries the device's shadow documents. */
  schema: 1 | 2 | 3 | 4;
  kind: 'fleet-data';
  chain_identifier: string;
  registry: string;
  fleet_id: string;
  device_id: string;
  signer: string;
  seq: number;
  observed_ms: number;
  reason: 'logs' | 'diagnostics' | 'crash';
  dropped_lines: number;
  records: ArchiveRecord[];
  /** Present exactly in schema 2. */
  shadows?: ShadowSnapshot;
  /** Schema 3: exact prior CompactHead BCS bytes, authenticated by the chain root. */
  previous_head?: number[];
  /** Schema 4: authenticated ciphertext descriptors; identifiers are locators. */
  items?: { identifier: string; size: number; sha256: string }[];
}
const ARCHIVE_FIELDS = ['schema', 'kind', 'chain_identifier', 'registry', 'fleet_id', 'device_id', 'signer', 'seq', 'observed_ms', 'reason', 'dropped_lines', 'records'];
export type Anchor = ReturnType<typeof AnchorBcs.parse> & { index?: number };
export const CompactHeadBcs = bcs.struct('CompactHead', {
  first_index: bcs.u64(), index: bcs.u64(), anchor: AnchorBcs, previous_hash: bcs.byteVector(),
});
export type CompactHead = ReturnType<typeof CompactHeadBcs.parse>;
export function parseCompactHead(bytes: Uint8Array): CompactHead {
  if (bytes.length > 512) throw Error('Compact archive root exceeds limit');
  const h = CompactHeadBcs.parse(bytes);
  if (BigInt(h.first_index) > BigInt(h.index) || BigInt(h.anchor.seq) === BigInt(0)
    || h.anchor.kind > 2 || h.anchor.ciphertext_sha256.length !== 32
    || ![0,32].includes(h.previous_hash.length)) throw Error('Invalid compact archive root');
  return h;
}
export async function readCompactHead(client: SuiGrpcClient, config: FleetDataConfig, writerId: string): Promise<CompactHead | undefined> {
  if (!config.compactHistoryOrigin) return undefined;
  const { object } = await client.getObject({ objectId: config.packageId, include: { objectBcs: true } });
  const raw = bcs.Object.parse(object.objectBcs).data.Package;
  for (const name of ['CompactHeadKey', 'CompactHead']) {
    const entries = raw?.typeOriginTable.filter(e => e.moduleName === 'fleet_data' && e.datatypeName === name);
    if (entries?.length !== 1 || id(entries[0].package) !== config.compactHistoryOrigin) throw Error('Compact archive origin mismatch');
  }
  try {
    const field = await client.getDynamicField({ parentId: writerId, name: {
      type: `${config.compactHistoryOrigin}::fleet_data::CompactHeadKey`, bcs: new Uint8Array([0]),
    } });
    if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(`${config.compactHistoryOrigin}::fleet_data::CompactHead`)) throw Error('Invalid compact archive root type');
    return parseCompactHead(field.dynamicField.value.bcs);
  } catch (error) {
    if (error instanceof ObjectError && error.reason === 'notFound') return undefined;
    throw error;
  }
}
/** Only a hash committed by the authenticated parent authorizes traversal. */
export async function previousCompactHead(archive: Archive, parent: CompactHead): Promise<CompactHead | undefined> {
  if (![3,4].includes(archive.schema) || !Array.isArray(archive.previous_head)) throw Error('Compact archive has no authenticated history');
  const previous = new Uint8Array(archive.previous_head);
  if (!previous.length) {
    if (parent.previous_hash.length) throw Error('Archive history was truncated');
    return undefined;
  }
  const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', previous));
  if (!equal(hash, parent.previous_hash)) throw Error('Archive predecessor commitment mismatch');
  const h = parseCompactHead(previous);
  if (BigInt(h.anchor.seq) >= BigInt(parent.anchor.seq) || BigInt(h.index) >= BigInt(parent.index)
    || h.first_index !== parent.first_index) throw Error('Archive history does not move backwards');
  return h;
}
const integer = (n: unknown): n is number => typeof n === 'number' && Number.isSafeInteger(n) && n >= 0;
const equal = (a: ArrayLike<number>, b: ArrayLike<number>) => a.length === b.length && Array.from(a).every((v, i) => v === b[i]);
const bytes = (s: string) => new TextEncoder().encode(s).length;
function parsePrivateJson(bytes: Uint8Array) {
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
  catch { throw Error('Invalid archive JSON'); } // JSON.parse errors can quote private log text.
}
const exact = (v: object, fields: string[]) => Object.keys(v).sort().join() === fields.sort().join();
export const archiveAad = (config: FleetDataConfig, anchor: Anchor) => new TextEncoder().encode(
  `suifleet/fleet-data/v1/${config.chainIdentifier}/${config.fleetId}/${anchor.seq}/${anchor.kind}`);

/** Fetch a bounded page from the chain's per-device index, never a relay index. */
export async function readAnchors(client: SuiGrpcClient, config: FleetDataConfig, deviceId: string, start = 0, limit = 32): Promise<Anchor[]> {
  await readFleetData(client, config);
  if (!Number.isSafeInteger(start) || start < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 64) throw Error('Invalid archive page');
  const writer = await readDeviceData(client, config, deviceId);
  if (!writer) return [];
  const count = BigInt(writer.anchor_count);
  const compact = await readCompactHead(client, config, writer.id);
  if (compact && (BigInt(compact.index) >= count || BigInt(compact.anchor.seq) > BigInt(writer.batch_seq))) throw Error('Compact archive root sequence mismatch');
  const anchors: Anchor[] = [];
  for (let index = BigInt(start); index < count && index < BigInt(start) + BigInt(limit); index++) {
    if (compact && index === BigInt(compact.index)) {
      anchors.push({ ...compact.anchor, index: Number(index) });
      continue;
    }
    let field;
    try { field = await client.getDynamicField({ parentId: writer.id, name: {
      type: `${config.typeOrigin}::fleet_data::AnchorKey`, bcs: bcs.struct('AnchorKey', { index: bcs.u64() }).serialize({ index }).toBytes(),
    } }); } catch (error) {
      if (compact && index >= BigInt(compact.first_index) && error instanceof ObjectError && error.reason === 'notFound') continue;
      throw error;
    }
    if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(`${config.typeOrigin}::fleet_data::Anchor`)) throw Error('Invalid archive index type');
    const anchor = AnchorBcs.parse(field.dynamicField.value.bcs);
    if (BigInt(anchor.seq) > BigInt(writer.batch_seq) || (anchors.length && BigInt(anchor.seq) <= BigInt(anchors[anchors.length - 1].seq))) throw Error('Archive index sequence mismatch');
    anchors.push(compact ? { ...anchor, index: Number(index) } : anchor);
  }
  return anchors;
}

/** Also used after fetching: the anchor's signer is historical chain authority,
 * so signer rotation preserves signature verification. Seal key-epoch rotation
 * denies old-epoch decryption while retaining anchors. Current revocation blocks reads
 * here; actual confidentiality remains the Move owner/support policy. */
export async function verifyArchive(plaintext: Uint8Array, config: FleetDataConfig, deviceId: string, anchor: Anchor): Promise<Archive> {
  if (plaintext.length > MAX_ARCHIVE_BYTES) throw Error('Archive exceeds size limit');
  const envelope = parsePrivateJson(plaintext);
  if (!envelope || !exact(envelope, ['message', 'signature']) || typeof envelope.message !== 'string' || typeof envelope.signature !== 'string') throw Error('Invalid signed archive envelope');
  const message = fromBase64(envelope.message), signature = fromBase64(envelope.signature);
  if ((signature[0] !== 0 && signature[0] !== 2) || !await isValidPersonalMessageSignature(message, envelope.signature, { address: anchor.signer })) throw Error('Archive signature mismatch');
  const a: Archive = parsePrivateJson(message);
  if (!a || typeof a !== 'object' || !(a.schema === 1 ? exact(a, ARCHIVE_FIELDS) : a.schema === 2 ? exact(a, [...ARCHIVE_FIELDS, 'shadows']) : a.schema === 3 ? exact(a, [...ARCHIVE_FIELDS, 'previous_head', ...(a.shadows === undefined ? [] : ['shadows'])]) : a.schema === 4 && exact(a, [...ARCHIVE_FIELDS, 'items', ...(a.previous_head === undefined ? [] : ['previous_head']), ...(a.shadows === undefined ? [] : ['shadows'])]))
    || a.kind !== 'fleet-data' || a.chain_identifier !== config.chainIdentifier || id(a.registry) !== config.registryId
    || id(a.fleet_id) !== config.fleetId || id(a.device_id) !== id(deviceId) || id(a.signer) !== id(anchor.signer)
    || !integer(a.seq) || a.seq === 0 || BigInt(a.seq) !== BigInt(anchor.seq)
    || !integer(a.observed_ms) || BigInt(a.observed_ms) > BigInt(anchor.at_ms) + BigInt(300_000)
    || !integer(a.dropped_lines) || !['logs', 'diagnostics', 'crash'].includes(a.reason)
    || ['logs', 'diagnostics', 'crash'][anchor.kind] !== a.reason
    || !Array.isArray(a.records) || (a.schema !== 4 && !a.records.length) || (a.schema === 4 && a.records.length !== 0) || a.records.length > MAX_ARCHIVE_RECORDS) throw Error('Archive binding/shape mismatch');
  for (const r of a.records) {
    if (!r || !exact(r, ['source', 'line']) || typeof r.source !== 'string' || bytes(r.source) > 160
      || !(r.source === 'daemon' || (r.source.startsWith('component:') && r.source.length > 10))
      || typeof r.line !== 'string' || bytes(r.line) > 1024 || /[\r\n]/.test(r.line)) throw Error('Invalid archive log record');
  }
  if (a.shadows !== undefined) parseShadowSnapshot(a.shadows);
  if (a.schema === 4) {
    if (!Array.isArray(a.items) || a.items.length < 2 || a.items.length > 16) throw Error('Invalid Quilt item count');
    let total = 0; const names = new Set<string>();
    for (const item of a.items) {
      if (!item || !exact(item, ['identifier','size','sha256']) || typeof item.identifier !== 'string'
        || !/^[a-zA-Z0-9_-]{1,64}$/.test(item.identifier) || item.identifier === 'manifest' || names.has(item.identifier)
        || !integer(item.size) || item.size < 1 || (total += item.size) > MAX_ARCHIVE_BYTES
        || typeof item.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(item.sha256)) throw Error('Invalid Quilt item descriptor');
      names.add(item.identifier);
    }
  }
  if (a.schema === 3 || a.previous_head !== undefined) {
    if (!Array.isArray(a.previous_head) || a.previous_head.length > 512
      || !a.previous_head.every(n => Number.isInteger(n) && n >= 0 && n <= 255)) throw Error('Invalid archive predecessor');
    if (a.previous_head.length && BigInt(parseCompactHead(new Uint8Array(a.previous_head)).anchor.seq) >= BigInt(a.seq)) throw Error('Replayed archive predecessor');
  }
  return a;
}

/** Bound streaming reads even when Content-Length is missing or dishonest. */
export async function fetchArchiveBytes(url: string, fetcher: typeof fetch = fetch): Promise<Uint8Array> {
  const response = await fetcher(url, { signal: AbortSignal.timeout(60_000), credentials: 'omit', referrerPolicy: 'no-referrer' });
  if (!response.ok || !response.body) throw Error('Archive unavailable');
  const reader = response.body.getReader(), chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > MAX_ARCHIVE_BYTES) throw Error('Archive exceeds size limit');
      chunks.push(value);
    }
  } finally { await reader.cancel(); }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
  return result;
}

/** Decryptor must use the fleet owner/support Seal policy and a signed session.
 * All format/namespace/epoch/AAD checks run before requesting any keys. */
export async function openArchive(ciphertext: Uint8Array, config: FleetDataConfig, deviceId: string, anchor: Anchor,
  decrypt: (encryptedObject: Uint8Array) => Promise<Uint8Array>): Promise<Archive> {
  if (ciphertext.length > MAX_ARCHIVE_BYTES || !isEnvelope(ciphertext)) throw Error('Archive must be Seal encrypted');
  if (anchor.ciphertext_sha256.length !== 32 || toHex(new Uint8Array(await crypto.subtle.digest('SHA-256', Uint8Array.from(ciphertext)))) !== toHex(new Uint8Array(anchor.ciphertext_sha256))) throw Error('Archive ciphertext digest mismatch');
  const encrypted = ciphertext.slice(MAGIC.length), object = EncryptedObject.parse(encrypted);
  const identity = Uint8Array.from(decodeHex(object.id));
  if (id(object.packageId) !== config.typeOrigin || identity.length < 56 || identity.length > 104
    || !equal(identity.slice(0, 32), bcs.Address.serialize(config.fleetId).toBytes())
    || !equal(identity.slice(32, 40), bcs.u64().serialize(anchor.key_epoch).toBytes())
    || !object.ciphertext.Aes256Gcm?.aad || !equal(object.ciphertext.Aes256Gcm.aad, archiveAad(config, anchor))) throw Error('Archive Seal policy binding mismatch');
  const plaintext = await decrypt(encrypted);
  try { return await verifyArchive(plaintext, config, deviceId, anchor); }
  finally { plaintext.fill(0); }
}
/** The archive's Seal identity or provider set differs from this deployment's
 * pinned settings: a configuration or tampering incident, never an outage. */
export class ArchiveSealBindingError extends Error {
  name = 'ArchiveSealBindingError';
}

function decodeHex(value: string): number[] {
  const hex = value.replace(/^0x/, '');
  if (!/^(?:[0-9a-f]{2})+$/i.test(hex)) throw new ArchiveSealBindingError('Invalid Seal identity');
  return hex.match(/../g)!.map(pair => parseInt(pair, 16));
}

export async function requireArchiveReadable(client: SuiGrpcClient, config: FleetDataConfig, deviceId: string, anchor: Anchor, currentEpoch: number) {
  const fleet = await readFleetData(client, config);
  const identity = await readIdentity(client, deviceId);
  if (!identity || identity.revoked) throw Error('Device is missing or revoked');
  if (!integer(currentEpoch) || BigInt(anchor.key_epoch) !== BigInt(fleet.key_epoch)) throw Error('Archive expired or key epoch revoked');
  // The immutable anchor records the original purchase, not later owner-paid
  // extensions. The fleet-owned live record is updated by SF-083 extend_blob
  // and removed by release_blob; never use the historical epoch as live expiry.
  const { object } = await client.getObject({ objectId: config.storageId, include: { content: true } });
  const [origin, module, name] = normalizeStructTag(object.type).split('::');
  if (module !== 'fleet_storage' || name !== 'FleetStorage' || object.owner.$kind !== 'Shared') throw Error('Invalid fleet storage');
  const storage = FleetStorageBcs.parse(object.content!);
  if (id(storage.id) !== config.storageId || id(storage.budget_id) !== config.budgetId || !['1', '2'].includes(storage.version)) throw Error('Foreign fleet storage');
  // RecordKey and BlobRecord were introduced with FleetStorage in interface v1.
  const field = await client.getDynamicField({ parentId: config.storageId, name: {
    type: `${origin}::fleet_storage::RecordKey`, bcs: bcs.struct('RecordKey', { blob_object_id: bcs.Address }).serialize({ blob_object_id: anchor.blob_object_id }).toBytes(),
  } });
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(`${origin}::fleet_storage::BlobRecord`)) throw Error('Invalid live archive record');
  const record = BlobRecordBcs.parse(field.dynamicField.value.bcs);
  if (!record.device_id || id(record.device_id) !== id(deviceId) || BigInt(record.blob_id) !== BigInt(anchor.blob_id)
    || record.end_epoch < anchor.end_epoch || currentEpoch >= record.end_epoch) throw Error('Archive expired, released or foreign');
}

/** Build a transaction kind for Seal evaluation only; never submits a chain write.
 * A session that expires before use throws Seal's `ExpiredSessionKeyError`, as
 * a key server would, so callers can ask for a new session rather than retry. */
export async function archiveDecryptor(client: SuiGrpcClient, config: FleetDataConfig, settings: SealSettings,
  sessionKey: SessionKey, authority: { kind: 'owner'; ownerCapId: string } | { kind: 'support' }) {
  if (sessionKey.isExpired() || id(sessionKey.getPackageId()) !== config.typeOrigin) throw Error('Expired or foreign Seal session');
  const seal = new SealClient({ suiClient: client, serverConfigs: settings.keyServers, verifyKeyServers: true });
  return async (encrypted: Uint8Array) => {
    if (sessionKey.isExpired()) throw new ExpiredSessionKeyError();
    const object = EncryptedObject.parse(encrypted);
    // Refuse a substituted weak or foreign provider set, even if it decrypted.
    const servers = settings.keyServers.flatMap(s => Array(s.weight).fill(id(s.objectId))).sort();
    const actual = object.services.map(([server]) => id(server)).sort();
    if (object.threshold !== settings.threshold || servers.join() !== actual.join()) throw new ArchiveSealBindingError('Archive Seal provider/threshold mismatch');
    const tx = new Transaction();
    tx.setSender(sessionKey.getAddress());
    if (authority.kind === 'owner') {
      tx.moveCall({ target: `${config.packageId}::fleet_data::seal_approve_owner`, arguments: [
        tx.pure.vector('u8', decodeHex(object.id)), tx.object(config.fleetId),
        tx.object(config.budgetId), tx.object(authority.ownerCapId),
      ] });
    } else {
      tx.moveCall({ target: `${config.packageId}::fleet_data::seal_approve_support`, arguments: [
        tx.pure.vector('u8', decodeHex(object.id)), tx.object(config.fleetId),
      ] });
    }
    const txBytes = await tx.build({ client, onlyTransactionKind: true });
    return seal.decrypt({ data: encrypted, sessionKey, txBytes, checkShareConsistency: true });
  };
}

/** Complete fail-closed read: select from the chain index, verify current
 * policy/revocation and Walrus epoch, fetch bounded ciphertext, then open it.
 * The Walrus client must use this deployment's pinned network configuration.
 * A relay can supply these bytes but never the identity, index or authority. */
export async function fetchVerifiedArchive(client: SuiGrpcClient, walrus: Pick<WalrusClient, 'systemState'> & Partial<Pick<WalrusClient, 'getBlob'>>,
  config: FleetDataConfig, deviceId: string, index: number, aggregator: string,
  decrypt: (encryptedObject: Uint8Array) => Promise<Uint8Array>, fetcher: typeof fetch = fetch): Promise<Archive> {
  if (!Number.isSafeInteger(index) || index < 0) throw Error('Invalid archive index');
  const writer = await readDeviceData(client, config, deviceId);
  const head = writer && await readCompactHead(client, config, writer.id);
  if (head && index >= Number(head.first_index) && index <= Number(head.index)) {
    let current: CompactHead | undefined = head;
    // A bounded request avoids unbounded downloads on malformed/huge histories.
    // Older retained history can be traversed in future paginated readers.
    for (let hops = 0; current && hops < 64; hops++) {
      const archive = await fetchAnchorArchive(client, walrus, config, deviceId, current.anchor, aggregator, decrypt, fetcher);
      const previous = await previousCompactHead(archive, current);
      if (Number(current.index) === index) return archive;
      if (Number(current.index) < index) break;
      current = previous;
    }
    throw Error('Archive unavailable or beyond the 64-link history read limit');
  }
  const [anchor] = await readAnchors(client, config, deviceId, index, 1);
  if (!anchor || (anchor.index !== undefined && anchor.index !== index)) throw Error('Archive index does not exist');
  return fetchAnchorArchive(client, walrus, config, deviceId, anchor, aggregator, decrypt, fetcher);
}

async function fetchAnchorArchive(client: SuiGrpcClient, walrus: Pick<WalrusClient, 'systemState'> & Partial<Pick<WalrusClient, 'getBlob'>>,
  config: FleetDataConfig, deviceId: string, anchor: Anchor, aggregator: string,
  decrypt: (encryptedObject: Uint8Array) => Promise<Uint8Array>, fetcher: typeof fetch): Promise<Archive> {
  const state = await walrus.systemState();
  await requireArchiveReadable(client, config, deviceId, anchor, state.committee.epoch);
  const { blobIdFromInt } = await import('@mysten/walrus');
  const url = new URL(aggregator);
  if (url.username || url.password || url.search || url.hash || (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))) throw Error('Invalid archive aggregator URL');
  let ciphertext = await fetchArchiveBytes(`${aggregator.replace(/\/+$/, '')}/v1/blobs/${blobIdFromInt(anchor.blob_id)}`, fetcher);
  let quilt: Awaited<ReturnType<WalrusClient['getBlob']>> | undefined;
  const patch = async (identifier: string, size?: number, sha256?: string) => {
    if (!walrus.getBlob) throw Error('Quilt reader unavailable');
    quilt ??= await walrus.getBlob({blobId:blobIdFromInt(anchor.blob_id)});
    const files = await quilt.files();
    if (files.length < 3 || files.length > 17) throw Error('Quilt index exceeds bounds');
    const matches = [];
    for (const file of files) if (await file.getIdentifier() === identifier) matches.push(file);
    if (matches.length !== 1) throw Error('Quilt patch unavailable or ambiguous');
    const bytes = await matches[0].bytes();
    if (bytes.length > MAX_ARCHIVE_BYTES || (size !== undefined && bytes.length !== size)
      || (sha256 !== undefined && toHex(new Uint8Array(await crypto.subtle.digest('SHA-256', Uint8Array.from(bytes)))) !== sha256)) throw Error('Quilt patch digest/size mismatch');
    return bytes;
  };
  if (!isEnvelope(ciphertext)) ciphertext = await patch('manifest');
  const archive = await openArchive(ciphertext, config, deviceId, anchor, decrypt);
  if (archive.schema === 4) {
    // Each patch repeats the signed chain/device/sequence/Seal bindings. Only
    // this verified manifest can authorize the expected ciphertext digest.
    const records: ArchiveRecord[] = [];
    for (const item of archive.items!) {
      const bytes = await patch(item.identifier, item.size, item.sha256);
      const itemAnchor = {...anchor, ciphertext_sha256: Array.from(decodeHex(item.sha256))};
      const opened = await openArchive(bytes, config, deviceId, itemAnchor, decrypt);
      if (opened.schema !== 1 || opened.observed_ms !== archive.observed_ms || opened.dropped_lines !== archive.dropped_lines) throw Error('Quilt patch manifest binding mismatch');
      records.push(...opened.records);
      if (records.length > MAX_ARCHIVE_RECORDS) throw Error('Quilt record quota exceeded');
    }
    archive.records = records;
  }
  // Recheck after potentially slow downloads and key requests. An outage,
  // revocation or key rotation while reading cannot return a trusted result.
  const refreshed = await walrus.systemState();
  await requireArchiveReadable(client, config, deviceId, anchor, refreshed.committee.epoch);
  return archive;
}
