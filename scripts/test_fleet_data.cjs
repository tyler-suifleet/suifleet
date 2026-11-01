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
function client({ f = fleet, w = writer, revoked = false, currentSigner = signer, origin = config.typeOrigin, indexType = '0x2::object::ID', fail = false, missingIndex = false, chainId = config.chainIdentifier, shared = true } = {}) {
 return {
  ledgerService: { async getServiceInfo() { return { response: { chainId: toBase58(Buffer.from(chainId + '00'.repeat(28), 'hex')) } }; } },
  async getObject({ objectId }) {
   if (fail) throw Error('fullnode unavailable');
   const isFleet = objectId === config.fleetId;
   return { object: { owner: { $kind: shared ? 'Shared' : 'AddressOwner' }, type: `${origin}::fleet_data::${isFleet ? 'FleetData' : 'DeviceData'}`,
    content: (isFleet ? data.FleetDataBcs : data.DeviceDataBcs).serialize(isFleet ? f : w).toBytes() } };
  },
  async getDynamicField({ parentId }) {
   if (parentId === config.fleetId && missingIndex) throw new (appRequire('@mysten/sui/client').ObjectError)(deviceId, 'missing', { reason: 'notFound' });
   return parentId === config.fleetId ? { dynamicField: { value: { type: indexType, bcs: bcs.Address.serialize(writerId).toBytes() } } }
    : { dynamicField: { value: { type: `${manifest.type_origins['device_registry::Identity']}::device_registry::Identity`,
     bcs: protocol.device_registry_IdentityBcs.serialize({ signer: currentSigner, revoked, owner_cap: id('0xc0') }).toBytes() } } };
  }
 };
}
(async () => {
 const now = snapshot.observed_ms + 1000;
 async function result(c) { return (await data.fetchChainHealth(c, config, [deviceId], now))[deviceId]; }
 const {Ed25519Keypair}=appRequire('@mysten/sui/keypairs/ed25519');
 const key=Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(7));
 const daily={...snapshot,interval_seconds:86400};
 const message=Buffer.from(JSON.stringify(daily));const signed=await key.signPersonalMessage(message);
 const dailyWriter={...writer,message:[...message],signature:bytes(signed.signature)};
 const dailyClient=client({f:{...fleet,health_interval_ms:'86400000'},w:dailyWriter});
 const day=86400000;
 assert.notEqual((await data.fetchChainHealth(dailyClient,config,[deviceId],snapshot.observed_ms+day/2))[deviceId].status,'unavailable');
 assert.notEqual((await data.fetchChainHealth(dailyClient,config,[deviceId],snapshot.observed_ms+day/2))[deviceId].status,'offline');
 assert.equal((await data.fetchChainHealth(dailyClient,config,[deviceId],snapshot.observed_ms+day*4))[deviceId].status,'offline');
 assert.throws(()=>data.validateHealthSnapshot({...daily,interval_seconds:86401},86400));
 // Exercise the actual detail/header components with real reader results.
 const React = appRequire('react'), { renderToStaticMarkup } = appRequire('react-dom/server');
 const { HealthDetails, TelemetrySettings } = load('components/DeviceHealth.tsx');
 const telemetry = load('lib/telemetry.ts');
 const absentConfig = (await telemetry.fetchFleetHealth(client(), [deviceId], id('0xa'), '', '', fetch, {}))[deviceId];
 const notMember = await result(client({ missingIndex: true }));
 const noReport = await result(client({ w: { ...writer, health_seq: '0', message: [], signature: [] } }));
 for (const [health, configured, expected] of [
  [absentConfig, false, 'Device health reporting is not set up for this fleet.'],
  [notMember, true, 'This device is not in the configured data fleet.'],
  [noReport, true, 'No health report yet for this device.'],
 ]) {
  const detail = renderToStaticMarkup(React.createElement(HealthDetails, { health }));
  const header = renderToStaticMarkup(React.createElement(TelemetrySettings, { configured }));
  assert.ok(detail.includes(expected), detail);
  assert.equal(header.includes('Health comes from verified device reports.'), configured);
  assert.equal(header.includes('not set up'), !configured);
  assert.doesNotMatch(detail + header, /NEXT_PUBLIC_/);
 }
 // A global locator must not make an unrelated selected fleet look configured.
 let selection = { supported: true, fleet: { budgetId: id('0x999') } };
 const scoped = loader({ '@/lib/fleet-data': { DATA_CONFIG: config, configuredDataFleets: () => [config] },
  './useFleetSelection': { useFleetSelection: () => selection } })('hooks/useFleetDataConfig.ts');
 assert.equal(scoped.useFleetDataConfig(), undefined);
 selection = { supported: true, fleet: { budgetId: config.budgetId } };
 assert.equal(scoped.useFleetDataConfig(), config);
 selection = { supported: false };
 assert.equal(scoped.useFleetDataConfig(), config);
 assert.equal(absentConfig.status, 'unavailable');
 assert.deepEqual(notMember.codes, ['not-in-data-fleet']);
 assert.deepEqual(noReport.codes, ['no-telemetry']);
 console.log('PASS: rendered health details/header distinguish missing config, missing data membership and no report yet');
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
