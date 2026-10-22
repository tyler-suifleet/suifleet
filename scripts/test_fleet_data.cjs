// Browser reader over exact on-chain BCS layouts, real device signatures and
// registry identities. No HTTP indexer is used or trusted by these tests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const cases = JSON.parse(fs.readFileSync('interfaces/telemetry-cases.json'));
const manifest = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
manifest.chain_identifier = cases.signed.snapshot.chain_identifier;
manifest.shared_objects.registry.id = cases.signed.snapshot.registry;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const { normalizeSuiAddress: id, toBase58 } = appRequire('@mysten/sui/utils');
const { bcs } = appRequire('@mysten/sui/bcs');
const load = loader();
const data = load('lib/fleet-data.ts');
const protocol = load('lib/fleet-protocol.ts');
const config = data.parseDataConfig(JSON.stringify({ schema: 1, chainIdentifier: manifest.chain_identifier,
 registryId: manifest.shared_objects.registry.id, packageId: id('0x81'), typeOrigin: id('0x81'),
 fleetId: id('0xf1'), budgetId: id('0xb1'), storageId: id('0x51') }));
const { snapshot, signer, envelope } = cases.signed;
const deviceId = snapshot.device_id, writerId = id('0xf2');
const bytes = s => Array.from(Buffer.from(s, 'base64'));
const fleet = { id: config.fleetId, version: '1', budget_id: config.budgetId, storage_id: config.storageId,
 registry_id: config.registryId, chain_identifier: config.chainIdentifier, key_epoch: '0', support: [],
 health_interval_ms: '3600000', batch_interval_ms: '86400000', emergency_interval_ms: '300000', device_count: '1' };
const writer = { id: writerId, version: '1', fleet_id: config.fleetId, device_id: deviceId, account_id: id('0xa1'),
 health_seq: String(snapshot.seq), health_at_ms: String(snapshot.observed_ms), message: bytes(envelope.message), signature: bytes(envelope.signature),
 batch_seq: '0', batch_at_ms: null, emergency_at_ms: null, anchor_count: '0' };
function client({ f = fleet, w = writer, revoked = false, currentSigner = signer, origin = config.typeOrigin, indexType = '0x2::object::ID', fail = false, chainId = config.chainIdentifier, shared = true } = {}) {
 return {
  ledgerService: { async getServiceInfo() { return { response: { chainId: toBase58(Buffer.from(chainId + '00'.repeat(28), 'hex')) } }; } },
  async getObject({ objectId }) {
   if (fail) throw Error('fullnode unavailable');
   const isFleet = objectId === config.fleetId;
   return { object: { owner: { $kind: shared ? 'Shared' : 'AddressOwner' }, type: `${origin}::fleet_data::${isFleet ? 'FleetData' : 'DeviceData'}`,
    content: (isFleet ? data.FleetDataBcs : data.DeviceDataBcs).serialize(isFleet ? f : w).toBytes() } };
  },
  async getDynamicField({ parentId }) {
   return parentId === config.fleetId ? { dynamicField: { value: { type: indexType, bcs: bcs.Address.serialize(writerId).toBytes() } } }
    : { dynamicField: { value: { type: `${manifest.type_origins['device_registry::Identity']}::device_registry::Identity`,
     bcs: protocol.device_registry_IdentityBcs.serialize({ signer: currentSigner, revoked, owner_cap: id('0xc0') }).toBytes() } } };
  }
 };
}
(async () => {
 const now = snapshot.observed_ms + 1000;
 async function result(c) { return (await data.fetchChainHealth(c, config, [deviceId], now))[deviceId]; }
 const valid = await result(client());
 assert.notEqual(valid.status, 'unavailable', JSON.stringify(valid));
 assert.equal(valid.observedMs, snapshot.observed_ms);
 assert.deepEqual(valid.components, snapshot.components);
 for (const [name, c] of [
  ['foreign endpoint chain', client({ chainId: 'deadbeef' })],
  ['nonshared data', client({ shared: false })],
  ['wrong chain', client({ f: { ...fleet, chain_identifier: 'deadbeef' } })],
  ['wrong registry', client({ f: { ...fleet, registry_id: id('0x99') } })],
  ['wrong budget', client({ f: { ...fleet, budget_id: id('0x99') } })],
  ['wrong storage', client({ f: { ...fleet, storage_id: id('0x99') } })],
  ['foreign type origin', client({ origin: id('0x99') })],
  ['foreign device', client({ w: { ...writer, device_id: id('0x99') } })],
  ['foreign fleet', client({ w: { ...writer, fleet_id: id('0x99') } })],
  ['replayed snapshot', client({ w: { ...writer, health_seq: String(snapshot.seq + 1) } })],
  ['future anchor', client({ w: { ...writer, health_at_ms: String(now + 300001) } })],
  ['tampered snapshot', client({ w: { ...writer, message: [...writer.message.slice(0, -1), 0] } })],
  ['rotated signer', client({ currentSigner: id('0x99') })],
  ['bad index type', client({ indexType: '0x2::object::UID' })],
  ['chain outage', client({ fail: true })],
 ]) assert.equal((await result(c)).status, 'unavailable', name);
 assert.equal((await result(client({ revoked: true }))).status, 'unreported');
 assert.equal((await data.fetchChainHealth(client(), config, [deviceId], now + Number(fleet.health_interval_ms) * 4))[deviceId].status, 'offline');
 assert.notEqual((await data.fetchChainHealth(client(), config, [deviceId], now + Number(fleet.health_interval_ms)))[deviceId].status, 'offline', 'hourly chain cadence must not use the device local sampling interval');
 assert.equal((await result(client({ f: { ...fleet, health_interval_ms: "1" } }))).status, 'unavailable');
 assert.throws(() => data.parseDataConfig(JSON.stringify({ ...config, chainIdentifier: 'deadbeef' })));
 assert.throws(() => data.validateHealthSnapshot({ ...snapshot, components: [{}] }));
 assert.throws(() => data.validateHealthSnapshot({ ...snapshot, unexpected: 'topic' }));
 assert.throws(() => data.validateHealthSnapshot({ ...snapshot, sync: { ...snapshot.sync, token: 'secret' } }));
 assert.throws(() => data.validateHealthSnapshot({ ...snapshot, seq: Number.MAX_SAFE_INTEGER + 1 }));
 assert.throws(() => data.validateHealthSnapshot({ ...snapshot, reasons: ['x'.repeat(201)] }));
 console.log('PASS: direct-chain health BCS, real signature/identity, origin/deployment binding, replay, tampering, revocation, outage and freshness');
})().catch(e => { console.error(e); process.exitCode = 1; });
