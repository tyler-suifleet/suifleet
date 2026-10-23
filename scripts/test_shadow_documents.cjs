// SF-084 encrypted shadow documents (schema-2 fleet archives) through the
// browser's verified read path: real SDK personal-message signatures and real
// Seal SDK encryption, with fixture key-server keys standing in for provider
// discovery. No chain, provider or wallet acceptance is claimed.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { normalizeSuiAddress: id, toBase58 } = appRequire('@mysten/sui/utils');
const { bcs } = appRequire('@mysten/sui/bcs');
const { EncryptedObject, SealClient, DemType } = appRequire('@mysten/seal');
const vectors = require('../interfaces/seal-vectors.json');
const shared = require('../interfaces/compat/archive-shadow-cases.json');
const { createDecipheriv, createHash } = require('node:crypto');
const fixture = require('../manifests/fixture.localnet.json');
fixture.chain_identifier = '4c78adac'; fixture.shared_objects.registry.id = id('0xa1');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(fixture);
const load = loader();
const bulk = load('lib/fleet-bulk.ts');
const data = load('lib/fleet-data.ts');
const budget = load('lib/fleet-budget.ts');
const protocol = load('lib/fleet-protocol.ts');
const shadows = load('lib/shadow-documents.ts');
const storageOrigin = id('0xb3');
const key = Ed25519Keypair.generate(), device = id('0xd1');
const config = { schema: 1, chainIdentifier: '4c78adac', registryId: id('0xa1'), packageId: id('0xa2'), typeOrigin: id('0xa2'), fleetId: id('0xf1'), budgetId: id('0xb1'), storageId: id('0xb2') };
const anchor = { seq: '8', kind: 0, blob_object_id: id('0xc1'), blob_id: '1', ciphertext_sha256: [], signer: key.toSuiAddress(), at_ms: '1000000', end_epoch: 100, key_epoch: '0' };
const snapshot = shared.cases[0].shadows;
const archive = { schema: 2, kind: 'fleet-data', chain_identifier: config.chainIdentifier, registry: config.registryId, fleet_id: config.fleetId, device_id: device,
  signer: key.toSuiAddress(), seq: 8, observed_ms: 1000000, reason: 'logs', dropped_lines: 0, records: [{ source: 'daemon', line: 'Shadow state snapshot' }], shadows: snapshot };
async function signed(value, signer = key) {
  const message = new TextEncoder().encode(JSON.stringify(value));
  const { signature } = await signer.signPersonalMessage(message);
  return new TextEncoder().encode(JSON.stringify({ message: Buffer.from(message).toString('base64'), signature }));
}

(async () => {
  // The same cases the daemon's validator runs (suifleet-edge fleet_data.rs).
  for (const c of shared.cases) {
    const value = JSON.parse(JSON.stringify(c.shadows));
    if (c.valid) assert.doesNotThrow(() => shadows.parseShadowSnapshot(value), c.name);
    else assert.throws(() => shadows.parseShadowSnapshot(value), /Invalid archived shadow/, c.name);
  }
  // A __proto__ key stays plain data: no prototype is replaced.
  const proto = JSON.parse(JSON.stringify(shared.cases.find(c => c.name.startsWith('a __proto__')).shadows));
  shadows.parseShadowSnapshot(proto);
  assert.equal(Object.getPrototypeOf(proto.documents[0].desired), Object.prototype);
  assert.equal(({}).a, undefined);
  console.log(`PASS: ${shared.cases.length} shared shadow-snapshot cases match the daemon; delta, order, nulls, depth, names, unknown fields`);

  // Signed schema-2 archive: exact binding and strict shape.
  const valid = await signed(archive);
  assert.deepEqual(await bulk.verifyArchive(valid, config, device, anchor), archive);
  const one = { ...archive, schema: 1 }; delete one.shadows;
  assert.deepEqual(await bulk.verifyArchive(await signed(one), config, device, anchor), one, 'schema 1 still verifies');
  for (const [name, value] of [
    ['schema 1 with shadows', { ...archive, schema: 1 }],
    ['schema 2 without shadows', one && { ...one, schema: 2 }],
    ['null shadows', { ...archive, shadows: null }],
    ['unknown schema', { ...archive, schema: 3 }],
    ['foreign fleet', { ...archive, fleet_id: id('0x99') }],
    ['foreign device', { ...archive, device_id: id('0x99') }],
    ['older archive replayed under a newer anchor', { ...archive, seq: 7 }],
    ['inconsistent delta', { ...archive, shadows: { ...snapshot, documents: [{ ...snapshot.documents[0], delta: {} }, snapshot.documents[1]] } }],
    ['secret field beside the documents', { ...archive, shadows: { ...snapshot, token: 'private' } }],
  ]) await assert.rejects(bulk.verifyArchive(await signed(value), config, device, anchor), undefined, name);
  // Changing a reported value after signing breaks the signature.
  const tampered = JSON.parse(new TextDecoder().decode(valid));
  const altered = JSON.parse(JSON.stringify(archive)); altered.shadows.documents[1].reported.uptime = 10;
  tampered.message = Buffer.from(JSON.stringify(altered)).toString('base64');
  await assert.rejects(bulk.verifyArchive(new TextEncoder().encode(JSON.stringify(tampered)), config, device, anchor), /signature/);
  await assert.rejects(bulk.verifyArchive(await signed(archive, Ed25519Keypair.generate()), config, device, anchor), /signature/);
  console.log('PASS: schema-2 archives bind shadow documents to chain/fleet/device/sequence/signer; tampered, replayed, foreign and cross-schema archives are refused');

  // Documents, newest only, imprecise numbers and schema 1.
  const view = shadows.shadowDocumentsView(archive, 4, 5);
  assert.equal(view.state, 'verified');
  assert.equal(view.newest, true);
  assert.deepEqual(view.documents.map(d => [d.state, d.name]), [['verified', ''], ['verified', 'cfg']]);
  assert.deepEqual(view.documents[0].delta, { fan: { speed: 2 } });
  assert.equal(shadows.shadowDocumentsView(archive, 3, 5).newest, false, 'an older archive is history, not the current document');
  assert.equal(shadows.shadowDocumentsView(one, 4, 5).state, 'unavailable');
  const big = JSON.parse('{"documents":[{"name":"big","version":1,"updated_ms":1,"desired":null,"reported":{"count":18446744073709551615},"delta":{}}],"omitted":0}');
  shadows.parseShadowSnapshot(big);
  const bigView = shadows.shadowDocumentsView({ ...archive, shadows: big }, 0, 1);
  assert.equal(bigView.documents[0].state, 'unavailable', 'a rounded integer is never shown as the device value');
  console.log('PASS: newest archive marked current, older archives marked history, imprecise documents and schema-1 archives unavailable');

  // Encrypted end to end through fetchVerifiedArchive.
  const KeyServer = bcs.struct('KeyServer', { id: bcs.Address, firstVersion: bcs.u64(), lastVersion: bcs.u64() });
  const KeyServerV1 = bcs.struct('KeyServerV1', { name: bcs.string(), url: bcs.string(), keyType: bcs.u8(), pk: bcs.byteVector() });
  const chain = { core: {
    async getObject({ objectId }) { return { object: objectId === config.typeOrigin ? { version: '1' } : { content: KeyServer.serialize({ id: objectId, firstVersion: 1, lastVersion: 1 }).toBytes() } }; },
    async getDynamicField({ parentId }) {
      const server = vectors.servers.find(s => s.object_id === parentId);
      return { dynamicField: { value: { bcs: KeyServerV1.serialize({ name: 'fixture', url: 'https://fixture.invalid', keyType: 0, pk: Buffer.from(server.public_key, 'hex') }).toBytes() } } };
    },
  } };
  const seal = new SealClient({ suiClient: chain, serverConfigs: vectors.servers.map(s => ({ objectId: s.object_id, weight: 1 })), verifyKeyServers: false });
  const identity = Buffer.concat([Buffer.from(config.fleetId.slice(2), 'hex'), Buffer.alloc(8), Buffer.alloc(32, 7)]).toString('hex');
  const { encryptedObject, key: demKey } = await seal.encrypt({ threshold: 2, packageId: config.typeOrigin, id: identity, data: valid, aad: bulk.archiveAad(config, anchor), demType: DemType.AesGcm256 });
  const ciphertext = Buffer.concat([Buffer.from('suifleet/seal/v1\n'), encryptedObject]);
  const encryptedAnchor = { ...anchor, ciphertext_sha256: Array.from(createHash('sha256').update(ciphertext).digest()) };
  let plaintexts = [];
  const decrypt = async encrypted => {
    const parsed = EncryptedObject.parse(encrypted), { blob, aad } = parsed.ciphertext.Aes256Gcm;
    const decipher = createDecipheriv('aes-256-gcm', demKey, Buffer.from([138,55,153,253,198,46,121,219,160,128,89,7,214,156,148,220]));
    decipher.setAAD(Buffer.from(aad)); decipher.setAuthTag(Buffer.from(blob.slice(-16)));
    const plain = Uint8Array.from(Buffer.concat([decipher.update(blob.slice(0, -16)), decipher.final()]));
    plaintexts.push(plain);
    return plain;
  };
  const writerId = id('0xf2');
  const fleet = { id: config.fleetId, version: '1', budget_id: config.budgetId, storage_id: config.storageId, registry_id: config.registryId,
    chain_identifier: config.chainIdentifier, key_epoch: '0', support: [], health_interval_ms: '3600000', batch_interval_ms: '86400000', emergency_interval_ms: '300000', device_count: '1' };
  const writer = { id: writerId, version: '1', fleet_id: config.fleetId, device_id: device, account_id: id('0xa3'), health_seq: '0', health_at_ms: '0', message: [], signature: [], batch_seq: '8', batch_at_ms: null, emergency_at_ms: null, anchor_count: '1' };
  function reads({ revoked = false, keyEpoch = '0', foreignRecord = false, foreignFleetIndex = false } = {}) {
    return {
      ledgerService: { async getServiceInfo() { return { response: { chainId: toBase58(Buffer.from(config.chainIdentifier + '00'.repeat(28), 'hex')) } }; } },
      async getObject({ objectId }) {
        if (objectId === config.storageId) return { object: { owner: { $kind: 'Shared' }, type: `${storageOrigin}::fleet_storage::FleetStorage`, content: budget.FleetStorageBcs.serialize({ id: config.storageId, version: '1', budget_id: config.budgetId, pool: null, wal: { value: '0' }, default_quota: { period_ms: '1', encoded_bytes_per_period: '1', uploads_per_period: '1', max_blob_size: '1', write_fees_per_period: '1' }, blob_count: '1', total_encoded_bytes: '1', total_write_fees: '1' }).toBytes() } };
        const isFleet = objectId === config.fleetId;
        return { object: { owner: { $kind: 'Shared' }, type: `${config.typeOrigin}::fleet_data::${isFleet ? 'FleetData' : 'DeviceData'}`,
          content: (isFleet ? data.FleetDataBcs : data.DeviceDataBcs).serialize(isFleet ? { ...fleet, key_epoch: keyEpoch } : { ...writer, fleet_id: foreignFleetIndex ? id('0x99') : config.fleetId }).toBytes() } };
      },
      async getDynamicField({ parentId }) {
        if (parentId === config.storageId) return { dynamicField: { value: { type: `${storageOrigin}::fleet_storage::BlobRecord`, bcs: bulk.BlobRecordBcs.serialize({ device_id: foreignRecord ? id('0xde') : device, blob_id: encryptedAnchor.blob_id, size: '1', encoded_size: '1', end_epoch: 100, uploaded_at_ms: '1' }).toBytes() } } };
        if (parentId === config.fleetId) return { dynamicField: { value: { type: '0x2::object::ID', bcs: bcs.Address.serialize(writerId).toBytes() } } };
        if (parentId === writerId) return { dynamicField: { value: { type: `${config.typeOrigin}::fleet_data::Anchor`, bcs: data.AnchorBcs.serialize(encryptedAnchor).toBytes() } } };
        return { dynamicField: { value: { type: `${fixture.type_origins['device_registry::Identity']}::device_registry::Identity`,
          bcs: protocol.device_registry_IdentityBcs.serialize({ signer: key.toSuiAddress(), revoked, owner_cap: id('0xc0') }).toBytes() } } };
      },
    };
  }
  const walrus = { async systemState() { return { committee: { epoch: 99 } }; } };
  const request = async () => new Response(ciphertext);
  const opened = await bulk.fetchVerifiedArchive(reads(), walrus, config, device, 0, 'https://fixture.invalid', decrypt, request);
  assert.deepEqual(opened.shadows, snapshot);
  assert.ok(plaintexts.length === 1 && plaintexts[0].every(b => b === 0), 'decrypted plaintext buffer is wiped after parsing');
  for (const [name, source] of [
    ['revoked device', reads({ revoked: true })], ['retired key epoch', reads({ keyEpoch: '1' })],
    ['foreign storage record', reads({ foreignRecord: true })], ['writer of a foreign fleet', reads({ foreignFleetIndex: true })],
  ]) await assert.rejects(bulk.fetchVerifiedArchive(source, walrus, config, device, 0, 'https://fixture.invalid', decrypt, request), undefined, name);
  console.log('PASS: Seal-encrypted shadow documents open only through the chain index with live revocation, key-epoch, storage and fleet checks');
})().catch(error => { console.error(error); process.exitCode = 1; });
