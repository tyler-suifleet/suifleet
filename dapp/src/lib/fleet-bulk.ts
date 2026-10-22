// Fleet archives authenticate the encrypted bytes on chain and the exact
// plaintext with the writer's personal-message signature. Never trust an
// aggregator, an optional relay, or a successful decryption as authentication.
import type { WalrusClient } from '@mysten/walrus';
import { bcs } from '@mysten/sui/bcs';
import { isValidPersonalMessageSignature } from '@mysten/sui/verify';
import { fromBase64, normalizeSuiAddress as id, normalizeStructTag, toHex } from '@mysten/sui/utils';
import { EncryptedObject, SealClient, type SessionKey } from '@mysten/seal';
import { Transaction } from '@mysten/sui/transactions';
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { AnchorBcs, readDeviceData, readFleetData, type FleetDataConfig } from './fleet-data';
import { readIdentity } from './telemetry';
import { MAGIC, isEnvelope, type SealSettings } from './seal';

export const MAX_ARCHIVE_BYTES = 4 * 1024 * 1024;
export const MAX_ARCHIVE_RECORDS = 4096;
export interface ArchiveRecord { source: string; line: string }
export interface Archive {
  schema: 1;
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
}
export type Anchor = ReturnType<typeof AnchorBcs.parse>;
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
  const anchors: Anchor[] = [];
  for (let index = BigInt(start); index < count && anchors.length < limit; index++) {
    const field = await client.getDynamicField({ parentId: writer.id, name: {
      type: `${config.typeOrigin}::fleet_data::AnchorKey`, bcs: bcs.struct('AnchorKey', { index: bcs.u64() }).serialize({ index }).toBytes(),
    } });
    if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(`${config.typeOrigin}::fleet_data::Anchor`)) throw Error('Invalid archive index type');
    const anchor = AnchorBcs.parse(field.dynamicField.value.bcs);
    if (BigInt(anchor.seq) > BigInt(writer.batch_seq) || (anchors.length && BigInt(anchor.seq) <= BigInt(anchors[anchors.length - 1].seq))) throw Error('Archive index sequence mismatch');
    anchors.push(anchor);
  }
  return anchors;
}

/** Also used after fetching: the anchor's signer is historical chain authority,
 * so key rotation does not erase prior history. Current revocation blocks reads
 * here; actual confidentiality remains the Move owner/support policy. */
export async function verifyArchive(plaintext: Uint8Array, config: FleetDataConfig, deviceId: string, anchor: Anchor): Promise<Archive> {
  if (plaintext.length > MAX_ARCHIVE_BYTES) throw Error('Archive exceeds size limit');
  const envelope = parsePrivateJson(plaintext);
  if (!envelope || !exact(envelope, ['message', 'signature']) || typeof envelope.message !== 'string' || typeof envelope.signature !== 'string') throw Error('Invalid signed archive envelope');
  const message = fromBase64(envelope.message), signature = fromBase64(envelope.signature);
  if ((signature[0] !== 0 && signature[0] !== 2) || !await isValidPersonalMessageSignature(message, envelope.signature, { address: anchor.signer })) throw Error('Archive signature mismatch');
  const a: Archive = parsePrivateJson(message);
  if (!a || !exact(a, ['schema', 'kind', 'chain_identifier', 'registry', 'fleet_id', 'device_id', 'signer', 'seq', 'observed_ms', 'reason', 'dropped_lines', 'records'])
    || a.schema !== 1 || a.kind !== 'fleet-data' || a.chain_identifier !== config.chainIdentifier || id(a.registry) !== config.registryId
    || id(a.fleet_id) !== config.fleetId || id(a.device_id) !== id(deviceId) || id(a.signer) !== id(anchor.signer)
    || !integer(a.seq) || a.seq === 0 || BigInt(a.seq) !== BigInt(anchor.seq)
    || !integer(a.observed_ms) || BigInt(a.observed_ms) > BigInt(anchor.at_ms) + BigInt(300_000)
    || !integer(a.dropped_lines) || !['logs', 'diagnostics', 'crash'].includes(a.reason)
    || ['logs', 'diagnostics', 'crash'][anchor.kind] !== a.reason
    || !Array.isArray(a.records) || !a.records.length || a.records.length > MAX_ARCHIVE_RECORDS) throw Error('Archive binding/shape mismatch');
  for (const r of a.records) {
    if (!r || !exact(r, ['source', 'line']) || typeof r.source !== 'string' || bytes(r.source) > 160
      || !(r.source === 'daemon' || (r.source.startsWith('component:') && r.source.length > 10))
      || typeof r.line !== 'string' || bytes(r.line) > 1024 || /[\r\n]/.test(r.line)) throw Error('Invalid archive log record');
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
function decodeHex(value: string): number[] {
  const hex = value.replace(/^0x/, '');
  if (!/^(?:[0-9a-f]{2})+$/i.test(hex)) throw Error('Invalid Seal identity');
  return hex.match(/../g)!.map(pair => parseInt(pair, 16));
}

export async function requireArchiveReadable(client: SuiGrpcClient, config: FleetDataConfig, deviceId: string, anchor: Anchor, currentEpoch: number) {
  const fleet = await readFleetData(client, config);
  const identity = await readIdentity(client, deviceId);
  if (!identity || identity.revoked) throw Error('Device is missing or revoked');
  if (!integer(currentEpoch) || currentEpoch >= anchor.end_epoch || BigInt(anchor.key_epoch) !== BigInt(fleet.key_epoch)) throw Error('Archive expired or key epoch revoked');
}

/** Build a transaction kind for Seal evaluation only; never submits a chain write. */
export async function archiveDecryptor(client: SuiGrpcClient, config: FleetDataConfig, settings: SealSettings,
  sessionKey: SessionKey, authority: { kind: 'owner'; ownerCapId: string } | { kind: 'support' }) {
  if (sessionKey.isExpired() || id(sessionKey.getPackageId()) !== config.typeOrigin) throw Error('Expired or foreign Seal session');
  const seal = new SealClient({ suiClient: client, serverConfigs: settings.keyServers, verifyKeyServers: true });
  return async (encrypted: Uint8Array) => {
    if (sessionKey.isExpired()) throw Error('Seal session expired');
    const object = EncryptedObject.parse(encrypted);
    // Refuse a substituted weak or foreign provider set, even if it decrypted.
    const servers = settings.keyServers.flatMap(s => Array(s.weight).fill(id(s.objectId))).sort();
    const actual = object.services.map(([server]) => id(server)).sort();
    if (object.threshold !== settings.threshold || servers.join() !== actual.join()) throw Error('Archive Seal provider/threshold mismatch');
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
export async function fetchVerifiedArchive(client: SuiGrpcClient, walrus: Pick<WalrusClient, 'systemState'>,
  config: FleetDataConfig, deviceId: string, index: number, aggregator: string,
  decrypt: (encryptedObject: Uint8Array) => Promise<Uint8Array>, fetcher: typeof fetch = fetch): Promise<Archive> {
  const [anchor] = await readAnchors(client, config, deviceId, index, 1);
  if (!anchor) throw Error('Archive index does not exist');
  const state = await walrus.systemState();
  await requireArchiveReadable(client, config, deviceId, anchor, state.committee.epoch);
  const { blobIdFromInt } = await import('@mysten/walrus');
  const url = new URL(aggregator);
  if (url.username || url.password || url.search || url.hash || (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))) throw Error('Invalid archive aggregator URL');
  const ciphertext = await fetchArchiveBytes(`${aggregator.replace(/\/+$/, '')}/v1/blobs/${blobIdFromInt(anchor.blob_id)}`, fetcher);
  const archive = await openArchive(ciphertext, config, deviceId, anchor, decrypt);
  // Recheck after potentially slow downloads and key requests. An outage,
  // revocation or key rotation while reading cannot return a trusted result.
  const refreshed = await walrus.systemState();
  await requireArchiveReadable(client, config, deviceId, anchor, refreshed.committee.epoch);
  return archive;
}
