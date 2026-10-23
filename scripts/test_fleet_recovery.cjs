// SF-091 browser-side recovery drill over the SF-082 verified readers.
// A clean operator environment restores only public configuration, discovers
// the fleet from the chain index and reads archives through real signatures,
// real Seal SDK ciphertext and independent AES-GCM opening. Chain responses,
// Walrus committee, aggregator and key servers are in-process fixtures with
// injected outages; no provider, public network or wallet acceptance is implied.
// Usage: node scripts/test_fleet_recovery.cjs [RECEIPT.json]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createDecipheriv, createHash } = require('node:crypto');

// A clean operator machine: fresh HOME/caches, no inherited dApp configuration.
for (const name of Object.keys(process.env)) if (name.startsWith('NEXT_PUBLIC_')) delete process.env[name];
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'suifleet-recovery-'));
Object.assign(process.env, { HOME: home, XDG_CONFIG_HOME: path.join(home, '.config'), XDG_CACHE_HOME: path.join(home, '.cache') });
const restoreStarted = performance.now();
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, '../interfaces/telemetry-cases.json')));
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../manifests/fixture.localnet.json')));
manifest.chain_identifier = cases.signed.snapshot.chain_identifier;
manifest.shared_objects.registry.id = cases.signed.snapshot.registry;
// Public, retained configuration files: the only inputs the restore needs.
const publicFiles = { manifest: JSON.stringify(manifest) };
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = publicFiles.manifest;

const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { normalizeSuiAddress: id, toBase58 } = appRequire('@mysten/sui/utils');
const { bcs } = appRequire('@mysten/sui/bcs');
const { ObjectError } = appRequire('@mysten/sui/client');
const { EncryptedObject, SealClient, DemType, NoAccessError, ExpiredSessionKeyError, TooManyFailedFetchKeyRequestsError, InvalidPTBError,
  InconsistentKeyServersError, InternalError } = appRequire('@mysten/seal');
const vectors = require('../interfaces/seal-vectors.json');
const load = loader();
const data = load('lib/fleet-data.ts');
const bulk = load('lib/fleet-bulk.ts');
const budget = load('lib/fleet-budget.ts');
const protocol = load('lib/fleet-protocol.ts');
const recovery = load('lib/fleet-recovery.ts');
const retention = load('lib/retention.ts');
const manifestModule = load('lib/deployment-manifest.ts');

const storageOrigin = id('0x5a');
const config = data.parseDataConfig(JSON.stringify({ schema: 1, chainIdentifier: manifest.chain_identifier,
  registryId: manifest.shared_objects.registry.id, packageId: id('0x81'), typeOrigin: id('0x81'),
  fleetId: id('0xf1'), budgetId: id('0xb1'), storageId: id('0x51') }));
const restoreMs = performance.now() - restoreStarted;
const { snapshot, signer: healthSigner, envelope } = cases.signed;
const now = snapshot.observed_ms + 1000;
const PRIVATE = 'sf091-private-log-line';
const archiveKey = Ed25519Keypair.generate();
const deviceA = id(snapshot.device_id), deviceB = id('0xdb'), deviceC = id('0xdc');
const timings = {};
const results = { scenarios: [] };
const record = (name, observed) => { results.scenarios.push({ name, ...observed }); };
const b64 = s => Array.from(Buffer.from(s, 'base64'));
const digest = input => Array.from(createHash('sha256').update(input).digest());
const magic = Buffer.from('suifleet/seal/v1\n');

// ── Fixture world ───────────────────────────────────────────────────────────
const world = {
  fleet: { id: config.fleetId, version: '1', budget_id: config.budgetId, storage_id: config.storageId, registry_id: config.registryId,
    chain_identifier: config.chainIdentifier, key_epoch: '0', support: [], health_interval_ms: '3600000', batch_interval_ms: '86400000',
    emergency_interval_ms: '300000', device_count: '3' },
  devices: {}, records: {}, ciphertexts: {}, keys: new Map(), identities: {},
  epoch: 99, faults: new Set(), keyRequests: 0, tamper: false, aggregatorStatus: 200,
};
function writerFor(device, index) {
  return { id: id(`0xe${index}`), version: '1', fleet_id: config.fleetId, device_id: device, account_id: id(`0xa${index}`),
    health_seq: '0', health_at_ms: '0', message: [], signature: [], batch_seq: '0', batch_at_ms: null, emergency_at_ms: null, anchor_count: '0' };
}
world.devices[deviceA] = { writer: { ...writerFor(deviceA, 1), health_seq: String(snapshot.seq), health_at_ms: String(snapshot.observed_ms),
  message: b64(envelope.message), signature: b64(envelope.signature) }, anchors: [] };
world.devices[deviceB] = { writer: writerFor(deviceB, 2), anchors: [] };
world.devices[deviceC] = { writer: writerFor(deviceC, 3), anchors: [] };
world.identities[deviceA] = { signer: healthSigner, revoked: false, owner_cap: id('0xc1') };
world.identities[deviceB] = { signer: id('0x99'), revoked: true, owner_cap: id('0xc2') };
world.identities[deviceC] = { signer: id('0x98'), revoked: false, owner_cap: id('0xc3') };

const KeyServer = bcs.struct('KeyServer', { id: bcs.Address, firstVersion: bcs.u64(), lastVersion: bcs.u64() });
const KeyServerV1 = bcs.struct('KeyServerV1', { name: bcs.string(), url: bcs.string(), keyType: bcs.u8(), pk: bcs.byteVector() });
const sealReads = { core: {
  async getObject({ objectId }) { return { object: objectId === config.typeOrigin ? { version: '1' } : { content: KeyServer.serialize({ id: objectId, firstVersion: 1, lastVersion: 1 }).toBytes() } }; },
  async getDynamicField({ parentId }) {
    const server = vectors.servers.find(s => s.object_id === parentId);
    return { dynamicField: { value: { bcs: KeyServerV1.serialize({ name: 'fixture', url: 'https://fixture.invalid', keyType: 0, pk: Buffer.from(server.public_key, 'hex') }).toBytes() } } };
  },
} };
const seal = new SealClient({ suiClient: sealReads, serverConfigs: vectors.servers.map(s => ({ objectId: s.object_id, weight: 1 })), verifyKeyServers: false });

async function addArchive(device, reason, endEpoch) {
  const entry = world.devices[device], w = entry.writer;
  const seq = Number(w.batch_seq) + 1, kind = ['logs', 'diagnostics', 'crash'].indexOf(reason);
  const blobObjectId = id(`0x${(0xb100 + Object.keys(world.records).length).toString(16)}`), blobId = String(1000 + Object.keys(world.records).length);
  const anchor = { seq: String(seq), kind, blob_object_id: blobObjectId, blob_id: blobId, ciphertext_sha256: [], signer: archiveKey.toSuiAddress(),
    at_ms: String(now - 1000), end_epoch: endEpoch, key_epoch: world.fleet.key_epoch };
  const archive = { schema: 1, kind: 'fleet-data', chain_identifier: config.chainIdentifier, registry: config.registryId, fleet_id: config.fleetId,
    device_id: device, signer: archiveKey.toSuiAddress(), seq, observed_ms: now - 2000, reason, dropped_lines: 0,
    records: [{ source: 'daemon', line: `${PRIVATE} ${device} ${seq}` }] };
  const message = new TextEncoder().encode(JSON.stringify(archive));
  const { signature } = await archiveKey.signPersonalMessage(message);
  const plaintext = new TextEncoder().encode(JSON.stringify({ message: Buffer.from(message).toString('base64'), signature }));
  const identity = Buffer.concat([Buffer.from(config.fleetId.slice(2), 'hex'), bcs.u64().serialize(world.fleet.key_epoch).toBytes(), Buffer.alloc(32, world.keys.size + 1)]).toString('hex'); // Unique nonce per archive.
  const { encryptedObject, key } = await seal.encrypt({ threshold: 2, packageId: config.typeOrigin, id: identity, data: plaintext, aad: bulk.archiveAad(config, anchor), demType: DemType.AesGcm256 });
  const ciphertext = Buffer.concat([magic, encryptedObject]);
  anchor.ciphertext_sha256 = digest(ciphertext);
  world.keys.set(EncryptedObject.parse(encryptedObject).id, key);
  world.ciphertexts[blobId] = ciphertext;
  world.records[blobObjectId] = { device_id: device, blob_id: blobId, size: '1', encoded_size: '1', end_epoch: endEpoch, uploaded_at_ms: '1' };
  entry.anchors.push(anchor);
  w.batch_seq = String(seq); w.anchor_count = String(entry.anchors.length);
  return anchor;
}

const rpcDown = () => { if (world.faults.has('rpc')) throw Error('UNAVAILABLE: fullnode connection refused'); };
const writerOwner = writerId => Object.values(world.devices).find(d => d.writer.id === writerId);
const shared = { $kind: 'Shared' };
const client = {
  ledgerService: { async getServiceInfo() { rpcDown(); return { response: { chainId: toBase58(Buffer.from(config.chainIdentifier + '00'.repeat(28), 'hex')) } }; } },
  async getObject({ objectId }) {
    rpcDown();
    if (objectId === config.fleetId) return { object: { owner: shared, type: `${config.typeOrigin}::fleet_data::FleetData`, content: data.FleetDataBcs.serialize(world.fleet).toBytes() } };
    if (objectId === config.storageId) return { object: { owner: shared, type: `${storageOrigin}::fleet_storage::FleetStorage`, content: budget.FleetStorageBcs.serialize({ id: config.storageId, version: '2', budget_id: config.budgetId, pool: null, wal: { value: '0' },
      default_quota: { period_ms: '1', encoded_bytes_per_period: '1', uploads_per_period: '1', max_blob_size: '1', write_fees_per_period: '1' }, blob_count: '1', total_encoded_bytes: '1', total_write_fees: '1' }).toBytes() } };
    const owner = writerOwner(objectId);
    if (owner) return { object: { owner: shared, type: `${config.typeOrigin}::fleet_data::DeviceData`, content: data.DeviceDataBcs.serialize(owner.writer).toBytes() } };
    throw new ObjectError('notFound', `object ${objectId} not found`, { reason: 'notFound', objectId });
  },
  async getDynamicField({ parentId: parent, name }) {
    rpcDown();
    const parentId = id(parent);
    const missing = () => { throw new ObjectError('notFound', 'dynamic field not found', { reason: 'notFound', objectId: parentId }); };
    if (parentId === config.fleetId) {
      const device = id(bcs.Address.parse(name.bcs)), entry = world.devices[device];
      return entry ? { dynamicField: { value: { type: '0x2::object::ID', bcs: bcs.Address.serialize(entry.writer.id).toBytes() } } } : missing();
    }
    if (parentId === config.storageId) {
      const blob = id(bcs.Address.parse(name.bcs)), value = world.records[blob];
      return value ? { dynamicField: { value: { type: `${storageOrigin}::fleet_storage::BlobRecord`, bcs: bulk.BlobRecordBcs.serialize(value).toBytes() } } } : missing();
    }
    if (parentId === id(manifest.shared_objects.registry.id)) {
      const value = world.identities[id(bcs.Address.parse(name.bcs))];
      return value ? { dynamicField: { value: { type: `${manifest.type_origins['device_registry::Identity']}::device_registry::Identity`,
        bcs: protocol.device_registry_IdentityBcs.serialize(value).toBytes() } } } : missing();
    }
    const owner = writerOwner(parentId);
    if (owner) {
      const anchor = owner.anchors[Number(bcs.u64().parse(name.bcs))];
      return anchor ? { dynamicField: { value: { type: `${config.typeOrigin}::fleet_data::Anchor`, bcs: data.AnchorBcs.serialize(anchor).toBytes() } } } : missing();
    }
    return missing();
  },
  // Two-entry pages exercise cursor paging during discovery.
  async listDynamicFields({ parentId, cursor }) {
    rpcDown();
    assert.equal(parentId, config.fleetId);
    const all = Object.keys(world.devices).map(device => ({ $kind: 'DynamicField', name: { type: `${config.typeOrigin}::fleet_data::DeviceKey`, bcs: bcs.Address.serialize(device).toBytes() }, valueType: '0x2::object::ID' }));
    const start = cursor ? Number(cursor) : 0, page = all.slice(start, start + 2);
    return { dynamicFields: page, cursor: String(start + page.length), hasNextPage: start + page.length < all.length };
  },
};
const walrus = { async systemState() { if (world.faults.has('walrus')) throw Error('Walrus committee read unavailable'); return { committee: { epoch: world.epoch } }; } };
const aggregator = 'https://aggregator.fixture.invalid';
const fetcher = async url => {
  if (world.faults.has('aggregator-network')) throw TypeError('fetch failed: aggregator unreachable');
  if (world.faults.has('aggregator-503')) return new Response('busy', { status: 503 });
  const blob = String(url).split('/').pop();
  const known = Object.keys(world.ciphertexts).find(b => (appRequire('@mysten/walrus').blobIdFromInt(b)) === blob);
  if (!known || world.faults.has('aggregator-404')) return new Response('not found', { status: 404 });
  const bytes = Buffer.from(world.ciphertexts[known]);
  if (world.tamper) bytes[bytes.length - 1] ^= 1;
  return new Response(bytes);
};
// Independent AES-GCM opening stands in for threshold key release.
const decrypt = async encrypted => {
  world.keyRequests++;
  if (world.faults.has('keyserver')) throw new TooManyFailedFetchKeyRequestsError('key servers unreachable');
  if (world.faults.has('denied')) throw new NoAccessError('user does not have access');
  if (world.faults.has('session')) throw new ExpiredSessionKeyError('session key expired');
  const parsed = EncryptedObject.parse(encrypted), { blob, aad } = parsed.ciphertext.Aes256Gcm;
  const decipher = createDecipheriv('aes-256-gcm', world.keys.get(parsed.id), Buffer.from([138,55,153,253,198,46,121,219,160,128,89,7,214,156,148,220]));
  decipher.setAAD(Buffer.from(aad)); decipher.setAuthTag(Buffer.from(blob.slice(-16)));
  return Uint8Array.from(Buffer.concat([decipher.update(blob.slice(0, -16)), decipher.final()]));
};
const assess = (device, index) => recovery.assessArchive(client, walrus, config, device, index, aggregator, decrypt, fetcher);

(async () => {
  assert.equal(manifestModule.requireManifest().chain_identifier, config.chainIdentifier, 'restored manifest binds the restored fleet configuration');
  assert.throws(() => data.parseDataConfig(JSON.stringify({ ...config, chainIdentifier: 'deadbeef' })), /another deployment/, 'a foreign restored configuration is refused');
  await addArchive(deviceA, 'logs', 100);
  await addArchive(deviceA, 'diagnostics', 100);
  await addArchive(deviceB, 'logs', 100);
  await addArchive(deviceC, 'logs', 50); // Storage already ended: never renewed.

  // Discovery from the chain index only, across pages, with the restored config.
  let started = performance.now();
  const devices = await recovery.discoverFleet(client, config);
  timings.discoveryMs = performance.now() - started;
  assert.deepEqual(devices.sort(), [deviceA, deviceB, deviceC].sort());
  await assert.rejects(recovery.discoverFleet(client, config, 1), /page bound/, 'a truncated discovery fails closed');
  const health = await data.fetchChainHealth(client, config, devices, now);
  assert.ok(['healthy', 'degraded'].includes(health[deviceA].status), JSON.stringify(health[deviceA]));
  assert.equal(health[deviceB].status, 'unreported', 'a revoked device is never shown healthy');
  record('clean restore and discovery', { devices: devices.length, pages: 2, restoreMs, discoveryMs: timings.discoveryMs, health: Object.fromEntries(devices.map(d => [d, health[d].status])) });

  // Baseline availability and permanent loss.
  const baseline = { a0: await assess(deviceA, 0), a1: await assess(deviceA, 1), b0: await assess(deviceB, 0), c0: await assess(deviceC, 0), a9: await assess(deviceA, 9) };
  assert.equal(baseline.a0.state, 'available', JSON.stringify(baseline.a0)); assert.equal(baseline.a1.state, 'available');
  assert.equal(baseline.a0.archive.records[0].line, `${PRIVATE} ${deviceA} 1`); assert.equal(baseline.a0.verified, 'plaintext');
  assert.deepEqual([baseline.b0.state, baseline.b0.recovery], ['device_revoked', 'not_recoverable']);
  assert.deepEqual([baseline.c0.state, baseline.c0.recovery], ['expired', 'not_recoverable']);
  assert.deepEqual([baseline.a9.state, baseline.a9.recovery], ['missing', 'incident']);
  record('baseline archive availability', Object.fromEntries(Object.entries(baseline).map(([k, v]) => [k, { state: v.state, recovery: v.recovery }])));

  // Resumable dependency outages: refusal attributed to the dependency, no
  // trusted result while down, verified read after it returns.
  for (const [fault, dependency, recoveryAction] of [['rpc', 'rpc', 'retry'], ['walrus', 'walrus', 'retry'], ['aggregator-503', 'aggregator', 'retry'],
    ['aggregator-network', 'aggregator', 'retry'], ['aggregator-404', 'aggregator', 'retry'], ['keyserver', 'keyserver', 'retry'], ['session', 'session', 'new_session']]) {
    world.faults.add(fault);
    const down = await assess(deviceA, 0);
    assert.deepEqual([down.state, down.dependency, down.recovery, down.archive], ['outage', dependency, recoveryAction, undefined], `${fault}: ${JSON.stringify(down)}`);
    if (fault === 'rpc') {
      const h = await data.fetchChainHealth(client, config, [deviceA], now);
      assert.equal(h[deviceA].status, 'unavailable', 'an RPC outage is unavailable, never offline or resolved');
      await assert.rejects(recovery.discoverFleet(client, config));
    }
    world.faults.delete(fault);
    started = performance.now();
    const up = await assess(deviceA, 0);
    const recoveredMs = performance.now() - started;
    assert.equal(up.state, 'available', `${fault} recovery`);
    record(`outage ${fault}`, { during: { state: down.state, dependency: down.dependency, recovery: down.recovery }, after: up.state, recoveryReadMs: recoveredMs, archivesLost: 0 });
  }

  // Authority failures are not outages: access denial needs an explicit grant.
  world.faults.add('denied');
  const denied = await assess(deviceA, 0);
  assert.deepEqual([denied.state, denied.recovery], ['access_denied', 'grant_access']);
  world.faults.delete('denied');
  record('key server access denied', { state: denied.state, recovery: denied.recovery });

  // The real SF-082 decryptor: a restored session that expires between
  // creation and use needs a new session, and a provider set that differs from
  // the pinned settings is an incident, never a key-server outage.
  const settings = { keyServers: vectors.servers.map(s => ({ objectId: s.object_id, weight: 1 })), threshold: 2 };
  let sessionExpired = false;
  const sessionKey = { isExpired: () => sessionExpired, getPackageId: () => config.typeOrigin, getAddress: () => id('0x5e') };
  const realDecrypt = await bulk.archiveDecryptor(client, config, settings, sessionKey, { kind: 'support' });
  sessionExpired = true;
  const expiredSession = await recovery.assessArchive(client, walrus, config, deviceA, 0, aggregator, realDecrypt, fetcher);
  assert.deepEqual([expiredSession.state, expiredSession.dependency, expiredSession.recovery], ['outage', 'session', 'new_session'], JSON.stringify(expiredSession));
  sessionExpired = false;
  const foreignProviders = await bulk.archiveDecryptor(client, config, { ...settings, threshold: 1 }, sessionKey, { kind: 'support' });
  const mismatch = await recovery.assessArchive(client, walrus, config, deviceA, 0, aggregator, foreignProviders, fetcher);
  assert.deepEqual([mismatch.state, mismatch.recovery, mismatch.dependency], ['invalid', 'incident', undefined], JSON.stringify(mismatch));
  assert.match(mismatch.reason, /ArchiveSealBindingError/);
  // Seal request/client refusals are incidents; only unavailability is retried.
  for (const [error, state] of [[new InvalidPTBError(), 'invalid'], [new InconsistentKeyServersError('inconsistent shares'), 'invalid'],
    [new InternalError(), 'outage'], [TypeError('fetch failed'), 'outage']]) {
    const thrown = await recovery.assessArchive(client, walrus, config, deviceA, 0, aggregator, async () => { throw error; }, fetcher);
    assert.equal(thrown.state, state, `${error.name}: ${JSON.stringify(thrown)}`);
  }
  record('real decryptor session expiry and provider binding', { expiredSession: { state: expiredSession.state, recovery: expiredSession.recovery },
    providerMismatch: { state: mismatch.state, recovery: mismatch.recovery } });

  // Tampered ciphertext is an incident and never requests keys.
  world.tamper = true;
  const requests = world.keyRequests;
  const tampered = await assess(deviceA, 0);
  assert.deepEqual([tampered.state, tampered.recovery], ['invalid', 'incident']);
  assert.equal(world.keyRequests, requests, 'tampered bytes fail before key release');
  const tamperedCiphertext = await recovery.assessArchive(client, walrus, config, deviceA, 0, aggregator, undefined, fetcher);
  assert.equal(tamperedCiphertext.state, 'invalid', 'ciphertext-only availability also detects tampering');
  world.tamper = false;
  record('tampered aggregator bytes', { state: tampered.state, recovery: tampered.recovery, keyRequests: 0 });
  // Availability without a Seal session: everything before key release.
  const ciphertextOnly = await recovery.assessArchive(client, walrus, config, deviceA, 0, aggregator, undefined, fetcher);
  assert.deepEqual([ciphertextOnly.state, ciphertextOnly.verified, ciphertextOnly.archive], ['available', 'ciphertext', undefined]);
  assert.equal(world.keyRequests, requests, 'ciphertext-only checks never request keys');
  assert.equal((await recovery.assessArchive(client, walrus, config, deviceC, 0, aggregator, undefined, fetcher)).state, 'expired');
  record('ciphertext availability without keys', { state: ciphertextOnly.state, verified: ciphertextOnly.verified, keyRequests: 0 });

  // Payment-failure retention drill (SF-092 facts over the same live record).
  const policy = { executor: id('0xe0'), margin: 2, wal: { value: '10' }, gas: { value: '10' }, wal_per_epoch: '10', gas_per_epoch: '10', reward: '1',
    epoch: 99, wal_spent: '0', gas_spent: '0', revision: '1', pending_wal: '100', pending_count: '1', covered_count: '4' };
  const blobs = Object.entries(world.records).map(([blob, r]) => ({ id: blob, end: r.end_epoch, encodedSize: 1n, policy: { target: 110, max_fee: '0', last_success_epoch: null } }));
  const depleted = retention.retentionFacts(policy, blobs, world.epoch);
  assert.ok(depleted.some(f => f.includes('underfunded')), 'protected reserve depletion is reported');
  assert.ok(depleted.some(f => f.includes('expired')), 'expired fleet content is reported as restoration, not renewal');
  assert.ok(depleted.some(f => f.includes('deadline approaching')), 'an unavailable executor/notifier leaves a visible deadline');
  // Failed funding purchase: no confirmed deposit, so balances and alerts do not change.
  const emptyBudget = { id: config.budgetId, capId: id('0xcb'), version: 1n, balance: 0n, paused: false, defaultLimits: budget.DEFAULT_LIMITS,
    lowBalanceThreshold: 10n, deviceCount: 3n, totalDeposited: 0n, totalWithdrawn: 0n, totalPaid: 0n, totalAllocated: 0n, totalReclaimed: 0n };
  const alertsBefore = budget.budgetAlerts(emptyBudget, [], BigInt(now), null);
  assert.ok(alertsBefore.some(a => a.text.includes('no unallocated funds')));
  assert.deepEqual(budget.budgetAlerts(emptyBudget, [], BigInt(now), null), alertsBefore, 'a declined checkout credits nothing');
  // Executor/notifier unavailable: epoch 100 arrives with no renewal.
  world.epoch = 100;
  const lapsed = await assess(deviceA, 0);
  assert.deepEqual([lapsed.state, lapsed.recovery], ['expired', 'not_recoverable']);
  // Top-up before expiry is modelled by the owner extension of the live record
  // in the other archive: it remains readable beyond its original anchor epoch.
  world.epoch = 99;
  world.records[world.devices[deviceA].anchors[1].blob_object_id].end_epoch = 120;
  world.epoch = 110;
  const extended = await assess(deviceA, 1);
  assert.equal(extended.state, 'available', 'owner top-up/extension before expiry keeps history readable');
  const lost = await assess(deviceA, 0);
  assert.deepEqual([lost.state, lost.recovery], ['expired', 'not_recoverable'], 'renewal after expiry cannot restore device archives');
  world.epoch = 99;
  record('payment failure retention', { reserveDepletion: 'reported', failedPurchaseCredit: 0, extendedBeforeExpiry: extended.state,
    lapsedWithoutRenewal: lapsed.state, archivesLost: ['device A seq 1 (logs)', 'device C seq 1 (logs)'], reconstructable: false });

  // Key-epoch rotation and compromised-device response are deliberate loss of
  // future reads, not outages.
  world.fleet.key_epoch = '1';
  const rotated = await assess(deviceA, 1);
  assert.deepEqual([rotated.state, rotated.recovery], ['key_epoch_rotated', 'not_recoverable']);
  world.fleet.key_epoch = '0';
  world.identities[deviceA] = { ...world.identities[deviceA], revoked: true };
  const revoked = await assess(deviceA, 1);
  assert.deepEqual([revoked.state, revoked.recovery], ['device_revoked', 'not_recoverable']);
  assert.equal((await data.fetchChainHealth(client, config, [deviceA], now))[deviceA].status, 'unreported');
  world.identities[deviceA] = { ...world.identities[deviceA], revoked: false };
  delete world.records[world.devices[deviceA].anchors[1].blob_object_id];
  const released = await assess(deviceA, 1);
  assert.deepEqual([released.state, released.recovery], ['released', 'not_recoverable']);
  record('rotation, revocation and release', { rotated: rotated.state, revoked: revoked.state, released: released.state });

  // Customer exit: public records only; no ciphertext, plaintext or keys.
  started = performance.now();
  const exported = await recovery.exportPublicRecords(client, config, devices, now);
  timings.exportMs = performance.now() - started;
  const text = JSON.stringify(exported);
  assert.equal(exported.devices.length, 3);
  assert.equal(exported.devices.find(d => d.deviceId === deviceA).anchors.length, 2);
  assert.equal(exported.devices.find(d => d.deviceId === deviceA).anchors[0].ciphertextSha256, Buffer.from(world.devices[deviceA].anchors[0].ciphertext_sha256).toString('hex'));
  for (const forbidden of [PRIVATE, 'suiprivkey', Buffer.from(world.ciphertexts['1000']).toString('base64').slice(0, 40)]) assert.ok(!text.includes(forbidden), `export leaks ${forbidden}`);
  record('customer public export', { devices: exported.devices.length, anchors: exported.devices.reduce((n, d) => n + d.anchors.length, 0), exportMs: timings.exportMs });

  // Resume management without duplicating deployments or trusting a foreign creator.
  const owner = id('0x0a'), stranger = id('0x0b');
  const event = (deploymentId, componentName, creator) => ({ control: { paused: false }, deploymentId, schemaVersion: '1', componentName, version: '1.0.0',
    recipeBlobId: `recipe-${componentName}`, artifactSha256: 'ab'.repeat(32), targetGroups: [], targetDevices: [deviceA], creator, timestampMs: 1, statuses: {}, receiptsVerified: true, resolvedDeviceIds: [deviceA] });
  const retained = ['app', 'sensor', 'agent'].map((componentName, index) => ({ ...(index === 0 ? { deploymentId: id('0xd0') } : {}), componentName, version: '1.0.0',
    recipeBlobId: `recipe-${componentName}`, artifactSha256: 'AB'.repeat(32), targetDevices: [deviceA], targetGroups: [] }));
  const plan = recovery.resumeDeployments(retained, [event(id('0xd0'), 'app', owner), event(id('0xd1'), 'sensor', owner), event(id('0xd2'), 'agent', stranger), event(id('0xd3'), 'extra', owner)], [owner]);
  assert.deepEqual(plan.present.map(p => p.deploymentId), [id('0xd0'), id('0xd1')], 'existing deployments are resumed, not recreated');
  assert.deepEqual(plan.missing.map(m => m.componentName), ['agent'], 'an untrusted creator never satisfies a retained deployment');
  assert.deepEqual(plan.unknown.map(u => u.deploymentId), [id('0xd3')]);
  record('resume without duplicate deployments', { present: plan.present.length, missing: plan.missing.length, unknownOnChain: plan.unknown.length });

  results.passed = true;
  results.boundary = 'In-process chain/Walrus/aggregator/key-server fixtures with real signatures and real Seal SDK ciphertext; not provider, public-network or wallet acceptance.';
  if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(results, null, 2) + '\n');
  console.log('PASS: clean restore/discovery, dependency outages with measured recovery, expiry/rotation/revocation/release loss, payment-failure retention, public export and duplicate-free resume');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => fs.rmSync(home, { recursive: true, force: true }));
