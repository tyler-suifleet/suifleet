// Real SDK personal-message signatures. No chain/provider acceptance claim.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { normalizeSuiAddress: id, toBase58 } = appRequire('@mysten/sui/utils');
const { bcs } = appRequire('@mysten/sui/bcs');
const { EncryptedObject, SealClient, DemType } = appRequire('@mysten/seal');
const vectors = require('../interfaces/seal-vectors.json');
const { createDecipheriv, createHash } = require('node:crypto');
const fixture = require('../manifests/fixture.localnet.json');
fixture.chain_identifier = '4c78adac'; fixture.shared_objects.registry.id = id('0xa1');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const load = loader();
const bulk = load('lib/fleet-bulk.ts');
const data = load('lib/fleet-data.ts');
const protocol = load('lib/fleet-protocol.ts');
const key = Ed25519Keypair.generate(), device = id('0xd1');
const config = { schema: 1, chainIdentifier: '4c78adac', registryId: id('0xa1'), packageId: id('0xa2'), typeOrigin: id('0xa2'), fleetId: id('0xf1'), budgetId: id('0xb1'), storageId: id('0xb2') };
const anchor = { seq: '8', kind: 0, blob_object_id: id('0xc1'), blob_id: '1', ciphertext_sha256: [], signer: key.toSuiAddress(), at_ms: '1000000', end_epoch: 100, key_epoch: '0' };
const archive = { schema: 1, kind: 'fleet-data', chain_identifier: config.chainIdentifier, registry: config.registryId, fleet_id: config.fleetId, device_id: device, signer: key.toSuiAddress(), seq: 8, observed_ms: 1000000, reason: 'logs', dropped_lines: 0, records: [{ source: 'daemon', line: 'redacted log' }] };
async function signed(value, signer = key) {
 const message = new TextEncoder().encode(JSON.stringify(value));
 const { signature } = await signer.signPersonalMessage(message);
 return new TextEncoder().encode(JSON.stringify({ message: Buffer.from(message).toString('base64'), signature }));
}
(async () => {
 const valid = await signed(archive);
 await assert.rejects(bulk.verifyArchive(new TextEncoder().encode('PRIVATE-CONTENT invalid JSON'), config, device, anchor), e => e.message === 'Invalid archive JSON');
 assert.deepEqual(await bulk.verifyArchive(valid, config, device, anchor), archive);
 for (const [name, edit] of [
  ['foreign chain', { chain_identifier: 'deadbeef' }], ['foreign registry', { registry: id('0x99') }],
  ['foreign fleet', { fleet_id: id('0x99') }], ['foreign device', { device_id: id('0x99') }],
  ['different signer claim', { signer: id('0x99') }], ['replay', { seq: 7 }], ['reason substitution', { reason: 'crash' }],
  ['future observation', { observed_ms: 1300001 }], ['unknown field', { token: 'private' }],
  ['invalid counter', { dropped_lines: -1 }], ['unsafe sequence', { seq: Number.MAX_SAFE_INTEGER + 1 }],
  ['empty archive', { records: [] }], ['oversize archive', { records: Array(4097).fill(archive.records[0]) }],
  ['invalid source', { records: [{ source: 'unknown', line: 'line' }] }],
  ['multiline', { records: [{ source: 'daemon', line: 'line\nline' }] }],
  ['oversize UTF8', { records: [{ source: 'daemon', line: 'é'.repeat(513) }] }],
  ['extra record fields', { records: [{ source: 'daemon', line: 'line', secret: 'private' }] }],
 ]) await assert.rejects(bulk.verifyArchive(await signed({ ...archive, ...edit }), config, device, anchor), undefined, name);
 await assert.rejects(bulk.verifyArchive(await signed(archive, Ed25519Keypair.generate()), config, device, anchor));
 const tampered = JSON.parse(new TextDecoder().decode(valid));
 tampered.message = Buffer.from(JSON.stringify({ ...archive, dropped_lines: 1 })).toString('base64');
 await assert.rejects(bulk.verifyArchive(new TextEncoder().encode(JSON.stringify(tampered)), config, device, anchor));
 // Real SDK encryption plus independent AES-GCM opening. Public fixture keys
 // replace provider discovery only; no key-server/network acceptance is implied.
 const KeyServer = bcs.struct('KeyServer', { id: bcs.Address, firstVersion: bcs.u64(), lastVersion: bcs.u64() });
 const KeyServerV1 = bcs.struct('KeyServerV1', { name: bcs.string(), url: bcs.string(), keyType: bcs.u8(), pk: bcs.byteVector() });
 const chain = { core: {
  async getObject({ objectId }) { return { object: objectId === config.typeOrigin ? { version: '1' } : { content: KeyServer.serialize({ id: objectId, firstVersion: 1, lastVersion: 1 }).toBytes() } }; },
  async getDynamicField({ parentId }) { const server = vectors.servers.find(s => s.object_id === parentId); return { dynamicField: { value: { bcs: KeyServerV1.serialize({ name: 'fixture', url: 'https://fixture.invalid', keyType: 0, pk: Buffer.from(server.public_key, 'hex') }).toBytes() } } }; },
 } };
 const client = new SealClient({ suiClient: chain, serverConfigs: vectors.servers.map(s => ({ objectId: s.object_id, weight: 1 })), verifyKeyServers: false });
 const identity = Buffer.concat([Buffer.from(config.fleetId.slice(2), 'hex'), Buffer.alloc(8), Buffer.alloc(32, 7)]).toString('hex');
 const { encryptedObject, key: demKey } = await client.encrypt({ threshold: 2, packageId: config.typeOrigin, id: identity, data: valid, aad: bulk.archiveAad(config, anchor), demType: DemType.AesGcm256 });
 const magic = Buffer.from('suifleet/seal/v1\n');
 const ciphertext = Buffer.concat([magic, encryptedObject]);
 const digest = input => Array.from(createHash('sha256').update(input).digest());
 const encryptedAnchor = { ...anchor, ciphertext_sha256: digest(ciphertext) };
 const decrypt = async encrypted => {
  const parsed = EncryptedObject.parse(encrypted), { blob, aad } = parsed.ciphertext.Aes256Gcm;
  const decipher = createDecipheriv('aes-256-gcm', demKey, Buffer.from([138,55,153,253,198,46,121,219,160,128,89,7,214,156,148,220]));
  decipher.setAAD(Buffer.from(aad)); decipher.setAuthTag(Buffer.from(blob.slice(-16)));
  return Uint8Array.from(Buffer.concat([decipher.update(blob.slice(0, -16)), decipher.final()]));
 };
 assert.deepEqual(await bulk.openArchive(ciphertext, config, device, encryptedAnchor, decrypt), archive);
 let keyRequests = 0;
 const forbidden = async () => { keyRequests++; throw Error('must not request keys'); };
 const changed = Buffer.from(ciphertext); changed[changed.length - 1] ^= 1;
 await assert.rejects(bulk.openArchive(changed, config, device, encryptedAnchor, forbidden), /digest/);
 for (const [name, mutate] of [
  ['namespace', o => { o.packageId = id('0x99'); }],
  ['policy', o => { o.id = '99'.repeat(32) + o.id.slice(64); }],
  ['epoch', o => { o.id = o.id.slice(0, 64) + '01' + o.id.slice(66); }],
  ['AAD', o => { o.ciphertext.Aes256Gcm.aad = new Uint8Array([1]); }],
 ]) {
  const object = EncryptedObject.parse(encryptedObject); mutate(object);
  const bad = Buffer.concat([magic, EncryptedObject.serialize(object).toBytes()]);
  await assert.rejects(bulk.openArchive(bad, config, device, { ...encryptedAnchor, ciphertext_sha256: digest(bad) }, forbidden), /policy binding/, name);
 }
 assert.equal(keyRequests, 0);
 const writerId = id('0xf2');
 const fleet = { id: config.fleetId, version: '1', budget_id: config.budgetId, storage_id: config.storageId, registry_id: config.registryId,
  chain_identifier: config.chainIdentifier, key_epoch: '0', support: [], health_interval_ms: '3600000', batch_interval_ms: '86400000', emergency_interval_ms: '300000', device_count: '1' };
 const writer = { id: writerId, version: '1', fleet_id: config.fleetId, device_id: device, account_id: id('0xa3'), health_seq: '0', health_at_ms: '0', message: [], signature: [], batch_seq: '8', batch_at_ms: null, emergency_at_ms: null, anchor_count: '1' };
 function reads({ revoked = false, keyEpoch = '0', badIndex = false, revokeAfter = false } = {}) {
  let identityReads = 0;
  return {
   ledgerService: { async getServiceInfo() { return { response: { chainId: toBase58(Buffer.from(config.chainIdentifier + '00'.repeat(28), 'hex')) } }; } },
   async getObject({ objectId }) { const isFleet = objectId === config.fleetId; return { object: { owner: { $kind: 'Shared' }, type: `${config.typeOrigin}::fleet_data::${isFleet ? 'FleetData' : 'DeviceData'}`, content: (isFleet ? data.FleetDataBcs : data.DeviceDataBcs).serialize(isFleet ? { ...fleet, key_epoch: keyEpoch } : writer).toBytes() } }; },
   async getDynamicField({ parentId }) {
    if (parentId === config.fleetId) return { dynamicField: { value: { type: '0x2::object::ID', bcs: bcs.Address.serialize(writerId).toBytes() } } };
    if (parentId === writerId) return { dynamicField: { value: { type: `${config.typeOrigin}::fleet_data::${badIndex ? 'Bad' : 'Anchor'}`, bcs: data.AnchorBcs.serialize(encryptedAnchor).toBytes() } } };
    identityReads++;
    return { dynamicField: { value: { type: `${fixture.type_origins['device_registry::Identity']}::device_registry::Identity`, bcs: protocol.device_registry_IdentityBcs.serialize({ signer: key.toSuiAddress(), revoked: revoked || (revokeAfter && identityReads > 1), owner_cap: id('0xc0') }).toBytes() } } };
   },
  };
 }
 const walrus = epoch => ({ async systemState() { return { committee: { epoch } }; } });
 const request = async () => new Response(ciphertext);
 assert.deepEqual(await bulk.fetchVerifiedArchive(reads(), walrus(99), config, device, 0, 'https://fixture.invalid', decrypt, request), archive);
 for (const [name, source, storage] of [
  ['revoked', reads({ revoked: true }), walrus(99)], ['key rotated', reads({ keyEpoch: '1' }), walrus(99)],
  ['expired', reads(), walrus(100)], ['invalid chain index', reads({ badIndex: true }), walrus(99)],
  ['revoked during download', reads({ revokeAfter: true }), walrus(99)],
 ]) await assert.rejects(bulk.fetchVerifiedArchive(source, storage, config, device, 0, 'https://fixture.invalid', decrypt, request), undefined, name);
 await assert.rejects(bulk.fetchVerifiedArchive(reads(), walrus(99), config, device, 1, 'https://fixture.invalid', decrypt, request), /does not exist/);
 await assert.rejects(bulk.fetchVerifiedArchive(reads(), walrus(99), config, device, 0, 'https://credential:secret@fixture.invalid', decrypt, request), /aggregator URL/);
 demKey.fill(0);
 let decrypted = false;
 await assert.rejects(bulk.openArchive(valid, config, device, anchor, async () => { decrypted = true; return valid; }));
 assert.equal(decrypted, false, 'unencrypted data must fail before requesting keys');
 const output = await bulk.fetchArchiveBytes('https://fixture.invalid', async () => new Response(valid));
 assert.deepEqual(output, valid);
 let cancelled = false;
 await assert.rejects(bulk.fetchArchiveBytes('https://fixture.invalid', async () => new Response(new ReadableStream({
  start(controller) { controller.enqueue(new Uint8Array(bulk.MAX_ARCHIVE_BYTES)); controller.enqueue(new Uint8Array(1)); },
  cancel() { cancelled = true; },
 }))));
 assert.equal(cancelled, true, 'oversize streaming response is cancelled');
 console.log('PASS: signed archives, chain/fleet/device/anchor binding, replay/tamper, UTF8/record/stream bounds, plaintext refusal');
})().catch(error => { console.error(error); process.exitCode = 1; });
