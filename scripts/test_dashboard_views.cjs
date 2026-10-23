// SF-084 dashboard views: device services, alert drill-down, canary rollout
// progress and archive log search. Health goes through the real SF-082 chain
// reader and signature verifier over exact BCS layouts; reports are signed
// with Mysten's SDK. Chain responses are fixtures.
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
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const load = loader();
const data = load('lib/fleet-data.ts');
const protocol = load('lib/fleet-protocol.ts');
const views = load('lib/device-services.ts');
const progress = load('lib/rollout-progress.ts');
const search = load('lib/archive-search.ts');

const config = data.parseDataConfig(JSON.stringify({ schema: 1, chainIdentifier: manifest.chain_identifier,
  registryId: manifest.shared_objects.registry.id, packageId: id('0x81'), typeOrigin: id('0x81'),
  fleetId: id('0xf1'), budgetId: id('0xb1'), storageId: id('0x51') }));
const key = Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(9)), signer = key.toSuiAddress();
const deviceId = id('0xd1'), writerId = id('0xf2');
const now = 1_800_000_000_000;
const services = {
  version: 2,
  messaging: { subscriptions: 2, queued: 5, refused: 1, dropped: 0, omitted: 0, queues: [
    { principal: 'sink', subscriptions: 1, queued: 5, fill_percent: 50, full_since_ms: null, refused: 1, refused_recent: 0, dropped: 0, dropped_recent: 0 }] },
  bridge: { connected: true, since_ms: now - 600_000, connects: 3, refused: 0, refused_recent: 0, rejected: 1, rejected_recent: 0, last_error: 'timeout' },
  streams: { omitted: 1, streams: [
    { name: 'events', bytes: 900, max_size: 1000, fill_percent: 90, messages: 12, oldest: 30, newest: 41, overwrite: false, rejected: 2, rejected_recent: 1,
      export: { checkpoint: 37, lag: 4, lost: 0, lost_recent: 0, batches: 7, failures: 3, consecutive_failures: 1, last_success_ms: now - 60_000, last_error: 'http-server-error' } },
    { name: 'local', bytes: 10, max_size: 1000, fill_percent: 1, messages: 1, oldest: 5, newest: 5, overwrite: true, rejected: 0, rejected_recent: 0, export: null }] },
  shadows: { shadows: 3, notifications_refused: 2, notifications_refused_recent: 1, omitted: 0,
    sync: [{ name: 'config', conflicts: 4, conflicts_recent: 1, failures: 0, consecutive_failures: 0, last_success_ms: now - 30_000, last_error: null }] },
};
const snapshot = (changes = {}) => ({ schema: 2, chain_identifier: manifest.chain_identifier, registry: manifest.shared_objects.registry.id,
  device_id: deviceId, signer, seq: 7, observed_ms: now - 10_000, interval_seconds: 60, daemon_version: '0.9.0', boot_id: null,
  started_ms: now - 3_600_000, dropped: 0, sync: { last_success_ms: now - 10_000, consecutive_failures: 0, last_error: null },
  pending_receipts: 0, components: [], health: 'healthy', reasons: [], services, ...changes });
const fleet = { id: config.fleetId, version: '1', budget_id: config.budgetId, storage_id: config.storageId, registry_id: config.registryId,
  chain_identifier: config.chainIdentifier, key_epoch: '0', support: [], health_interval_ms: '3600000', batch_interval_ms: '86400000',
  emergency_interval_ms: '300000', device_count: '1' };
async function writer(s, by = key, atMs = now - 5_000) {
  const message = new TextEncoder().encode(JSON.stringify(s));
  const { signature } = await by.signPersonalMessage(message);
  return { id: writerId, version: '1', fleet_id: config.fleetId, device_id: deviceId, account_id: id('0xa1'),
    health_seq: String(s.seq), health_at_ms: String(atMs), message: Array.from(message), signature: Array.from(Buffer.from(signature, 'base64')),
    batch_seq: '0', batch_at_ms: null, emergency_at_ms: null, anchor_count: '0' };
}
function client(w, { revoked = false } = {}) {
  return {
    ledgerService: { async getServiceInfo() { return { response: { chainId: toBase58(Buffer.from(config.chainIdentifier + '00'.repeat(28), 'hex')) } }; } },
    async getObject({ objectId }) {
      const isFleet = objectId === config.fleetId;
      return { object: { owner: { $kind: 'Shared' }, type: `${config.typeOrigin}::fleet_data::${isFleet ? 'FleetData' : 'DeviceData'}`,
        content: (isFleet ? data.FleetDataBcs : data.DeviceDataBcs).serialize(isFleet ? fleet : w).toBytes() } };
    },
    async getDynamicField({ parentId }) {
      return parentId === config.fleetId ? { dynamicField: { value: { type: '0x2::object::ID', bcs: bcs.Address.serialize(writerId).toBytes() } } }
        : { dynamicField: { value: { type: `${manifest.type_origins['device_registry::Identity']}::device_registry::Identity`,
          bcs: protocol.device_registry_IdentityBcs.serialize({ signer, revoked, owner_cap: id('0xc0') }).toBytes() } } };
    },
  };
}
const read = async (w, options) => (await data.fetchChainHealth(client(w, options), config, [deviceId], now))[deviceId];
const view = async (w, options) => views.deviceServicesView(await read(w, options));

(async () => {
  // Verified path: every service section, with the honest gaps marked.
  const v = await view(await writer(snapshot()));
  assert.equal(v.state, 'verified', JSON.stringify(v));
  assert.equal(v.observedMs, now - 10_000); assert.equal(v.receivedMs, now - 5_000);
  assert.equal(v.messaging.state, 'verified'); assert.equal(v.messaging.queues[0].principal, 'sink');
  assert.equal(v.bridge.state, 'verified'); assert.equal(v.bridge.last_error, 'timeout');
  assert.equal(v.streams.streams.length, 2); assert.equal(v.streams.omitted, 1);
  assert.deepEqual(v.streams.streams[0].exportState, { state: 'exported', health: services.streams.streams[0].export,
    checkpoint: { state: 'verified', checkpoint: 37 } });
  assert.deepEqual(v.streams.streams[1].exportState, { state: 'no-export' });
  assert.deepEqual(v.streams.streams.map(s => s.positions), [{ state: 'verified', oldest: 30, newest: 41 }, { state: 'verified', oldest: 5, newest: 5 }]);
  assert.equal(v.shadows.shadows, 3); assert.equal(v.shadows.sync[0].conflicts_recent, 1);
  // Documents are never in public health: they are in the encrypted archive.
  assert.equal(v.shadows.documents.state, 'encrypted');
  assert.match(v.shadows.documents.reason, /encrypted fleet archive/);
  // Services version 1 (an older daemon): the same values, positions unavailable.
  const v1services = JSON.parse(JSON.stringify(services)); v1services.version = 1;
  for (const st of v1services.streams.streams) { delete st.oldest; delete st.newest; if (st.export) delete st.export.checkpoint; }
  const old = await view(await writer(snapshot({ services: v1services })));
  assert.equal(old.state, 'verified');
  assert.match(old.streams.streams[0].positions.reason, /services version 1/);
  assert.equal(old.streams.streams[0].exportState.checkpoint.state, 'unavailable');
  assert.equal(old.streams.streams[0].exportState.health.lag, 4);
  // An empty stream reports no range; a checkpoint may outlive discarded messages.
  const emptied = JSON.parse(JSON.stringify(services));
  Object.assign(emptied.streams.streams[0], { messages: 0, bytes: 0, fill_percent: 0, oldest: null, newest: null });
  emptied.streams.streams[0].export.lag = 0;
  const empty = await view(await writer(snapshot({ services: emptied })));
  assert.deepEqual([empty.streams.streams[0].positions, empty.streams.streams[0].exportState.checkpoint],
    [{ state: 'verified', oldest: null, newest: null }, { state: 'verified', checkpoint: 37 }]);
  // A range inconsistent with the message count is refused as a whole.
  const reversed = JSON.parse(JSON.stringify(services)); reversed.streams.streams[0].oldest = 42;
  assert.equal((await view(await writer(snapshot({ services: reversed })))).state, 'unavailable');
  const disabled = await view(await writer(snapshot({ services: { version: 1, messaging: null, bridge: null, streams: null, shadows: null } })));
  for (const name of ['messaging', 'bridge', 'streams', 'shadows']) assert.equal(disabled[name].state, 'disabled', name);

  // Nothing unverifiable, stale or partial yields values.
  const unavailable = async (w, pattern, options) => {
    const r = await view(w, options);
    assert.equal(r.state, 'unavailable', JSON.stringify(r)); assert.match(r.reason, pattern);
    assert.equal(Object.keys(r).sort().join(), 'reason,state', 'an unavailable view must carry no values');
  };
  await unavailable(await writer(snapshot(), Ed25519Keypair.fromSecretKey(new Uint8Array(32).fill(3))), /could not be verified/);
  await unavailable(await writer(snapshot({ services: { ...services, bridge: { ...services.bridge, last_error: 'host broker.internal unreachable' } } })), /could not be verified/);
  await unavailable(await writer(snapshot({ services: { ...services, extra: 1 } })), /could not be verified/);
  await unavailable(await writer(snapshot({ schema: 1, services: undefined })), /no services section/);
  await unavailable(await writer(snapshot({ observed_ms: now - 4 * 3_600_000 - 10_000 }), key, now - 4 * 3_600_000), /too old/);
  await unavailable(await writer(snapshot({ observed_ms: now - 3 * 3_600_000 })), /buffered/);
  await unavailable(await writer(snapshot()), /No verified report/, { revoked: true });
  const tampered = await writer(snapshot()); tampered.message[10] ^= 1;
  await unavailable(tampered, /could not be verified/);
  const failing = { ...client(await writer(snapshot())), async getObject() { throw Error('fullnode unavailable'); } };
  assert.equal(views.deviceServicesView((await data.fetchChainHealth(failing, config, [deviceId], now))[deviceId]).state, 'unavailable');
  assert.equal(views.deviceServicesView(undefined).state, 'unavailable');
  // A value that bypassed the verifier is refused too.
  const healthy = await read(await writer(snapshot()));
  assert.equal(views.deviceServicesView({ ...healthy, services: { ...services, version: 3 } }).state, 'unavailable');
  assert.equal(views.deviceServicesView({ ...healthy, services: { ...services, version: 1 } }).state, 'unavailable');
  assert.equal(views.deviceServicesView({ ...healthy, status: 'degraded', codes: ['device-degraded'] }).state, 'verified');

  // Alert drill-down: one device's items; an unavailable fleet result stays unavailable.
  const alert = (deviceId, kind) => ({ key: `${kind}:${deviceId}:`, kind, severity: 'warning', deviceId, deviceName: 'pi', message: kind, action: 'act' });
  const ok = { status: 'ok', serverNowMs: now, items: [alert(deviceId, 'device-offline'), alert(id('0xd2'), 'shadow-conflict'), alert('0xd1', 'export-failing')] };
  assert.deepEqual(views.deviceAlerts(ok, '0x00d1').items.map(a => a.kind), ['device-offline', 'export-failing']);
  assert.deepEqual(views.deviceAlerts({ status: 'unavailable', reason: 'Chain alerts could not be verified' }, deviceId),
    { state: 'unavailable', reason: 'Chain alerts could not be verified' });
  assert.equal(views.utc(8.64e15 + 1), 'UTC milliseconds 8640000000000001 (out of range)');

  // Rollout progress from exact on-chain BCS.
  const m = n => id(`0x${(0xe0 + n).toString(16)}`);
  const policy = { concurrency: '2', rate_count: '2', rate_window_ms: '60000', bake_ms: '300000', minimum_samples: '2', failure_bps: '2500', offline_timeout_ms: '600000' };
  const record = (changes = {}) => protocol.deployment_manager_RolloutBcs.parse(protocol.deployment_manager_RolloutBcs.serialize({ policy,
    members: [m(1), m(2), m(3), m(4), m(5), m(6), m(7)], cohorts: ['0', '0', '1', '1', '1', '2', '2'],
    admitted_at: ['100', '100', '200', '0', '0', '0', '0'], outcomes: [1, 1, 2, 0, 0, 0, 0], skipped: [false, false, false, false, true, false, false],
    cohort: '1', state: 0, bake_started_ms: '0', progress_ms: '200', revision: '5', ...changes }).toBytes());
  const p = progress.rolloutProgress(record(), now);
  assert.equal(p.state, 'Active'); assert.equal(p.aborted, false); assert.equal(p.currentCohort, 1);
  assert.deepEqual(p.stages.map(s => [s.status, s.members, s.granted, s.inflight, s.applied, s.failed, s.skipped, s.waiting]),
    [['done', 2, 2, 0, 2, 0, 0, 0], ['current', 3, 1, 0, 0, 1, 1, 1], ['pending', 2, 0, 0, 0, 0, 0, 2]]);
  assert.equal(p.samples, 3); assert.equal(p.failures, 1); assert.equal(p.thresholdReached, true, '1/3 >= 25%');
  assert.equal(progress.bpsLabel(p.policy.failureBps), '25%'); assert.equal(progress.bpsLabel(BigInt(3333)), '33.33%'); assert.equal(progress.bpsLabel(BigInt(1050)), '10.5%');
  assert.equal(p.members[4].status, 'skipped'); assert.equal(p.members[2].grantedAtMs, BigInt(200));
  const aborted = progress.rolloutProgress(record({ state: 2 }), now);
  assert.equal(aborted.aborted, true); assert.equal(aborted.state, 'Aborted'); assert.equal(aborted.bake, null);
  const baking = progress.rolloutProgress(record({ outcomes: [1, 1, 1, 0, 0, 0, 0], bake_started_ms: String(now - 100_000) }), now);
  assert.equal(baking.thresholdReached, false); assert.equal(baking.bake.remainingMs, BigInt(200_000));
  assert.equal(progress.rolloutProgress(record({ bake_started_ms: String(now - 400_000) }), now).bake.remainingMs, BigInt(0));
  const complete = progress.rolloutProgress(record({ state: 3, cohort: '2' }), now);
  assert.ok(complete.stages.every(s => s.status === 'done'));
  for (const [name, change] of [
    ['length mismatch', { outcomes: [1, 1, 2, 0, 0, 0] }],
    ['outcome without grant', { outcomes: [1, 1, 2, 1, 0, 0, 0] }],
    ['admitted skip', { skipped: [true, false, false, false, false, false, false] }],
    ['unordered cohorts', { cohorts: ['0', '0', '2', '2', '2', '2', '2'] }],
    ['first cohort', { cohorts: ['1', '1', '1', '1', '1', '1', '1'] }],
    ['later-cohort grant', { admitted_at: ['100', '100', '200', '0', '0', '300', '0'] }],
    ['missing cohort', { cohort: '3' }],
    ['duplicate member', { members: [m(1), m(1), m(3), m(4), m(5), m(6), m(7)] }],
    ['unknown state', { state: 4 }],
    ['unknown outcome', { outcomes: [1, 1, 3, 0, 0, 0, 0] }],
    ['policy bounds', { policy: { ...policy, failure_bps: '10001' } }],
    ['small cohort', { policy: { ...policy, minimum_samples: '3' } }],
  ]) assert.throws(() => progress.rolloutProgress(record(change), now), undefined, name);

  // Decision audit: only this rollout, newest first, consistent revisions.
  const decision = (deployment, revision, state = 0, reason = 'admitted online cohort members') => ({ bcs: protocol.deployment_manager_RolloutDecisionBcs.serialize({
    deployment_id: deployment, revision: String(revision), cohort: '1', state, reason, at_ms: String(revision * 1000), admitted: [m(3)] }).toBytes() });
  const rollout = id('0xab');
  const decisions = progress.rolloutDecisions([decision(rollout, 2), decision(id('0xac'), 9), decision(rollout, 5, 2, 'failure threshold reached; permanently aborted'), decision(rollout, 1)], rollout, BigInt(5));
  assert.deepEqual(decisions.map(d => d.revision), [BigInt(5), BigInt(2), BigInt(1)]);
  assert.equal(decisions[0].state, 'Aborted'); assert.match(decisions[0].reason, /permanently aborted/);
  assert.throws(() => progress.rolloutDecisions([decision(rollout, 6)], rollout, BigInt(5)), /inconsistent/);
  assert.throws(() => progress.rolloutDecisions([decision(rollout, 2), decision(rollout, 2)], rollout, BigInt(5)), /inconsistent/);
  const pages = [[decision(rollout, 5)], [decision(rollout, 4)], [decision(rollout, 3)]];
  const calls = [];
  const events = { async listEvents(args) { calls.push(args); const i = calls.length - 1;
    return { events: pages[i], hasNextPage: i < pages.length - 1, endCursor: `c${i}` }; } };
  const all = await progress.fetchRolloutDecisions(events, rollout, BigInt(5));
  assert.equal(all.complete, true); assert.equal(all.decisions.length, 3);
  assert.equal(calls[0].filter.eventType, `${manifest.type_origins['deployment_manager::RolloutDecision']}::deployment_manager::RolloutDecision`);
  calls.length = 0;
  const partial = await progress.fetchRolloutDecisions(events, rollout, BigInt(5), 2);
  assert.equal(partial.complete, false); assert.equal(partial.decisions.length, 2);
  calls.length = 0;
  await assert.rejects(progress.fetchRolloutDecisions({ async listEvents() { return { events: [], hasNextPage: true, endCursor: 'same' }; } }, rollout, BigInt(5)), /no progress/);

  // Archive search over verified records: literal substring, bounded, no persistence.
  const archive = { records: [{ source: 'daemon', line: 'boot ok' }, { source: 'component:app', line: 'ERROR disk (a+b)' },
    { source: 'component:app', line: 'error retry' }, { source: 'daemon', line: 'shutdown' }] };
  const q = (text, extra = {}) => search.searchArchive(archive, { text, source: '', caseSensitive: false, ...extra });
  assert.deepEqual(q('error').matches.map(r => r.index), [1, 2]);
  assert.deepEqual(q('ERROR', { caseSensitive: true }).matches.map(r => r.index), [1]);
  assert.deepEqual(q('(a+b)').matches.map(r => r.index), [1], 'regex metacharacters are literal');
  assert.deepEqual(q('', { source: 'daemon' }).matches.map(r => r.line), ['boot ok', 'shutdown']);
  assert.equal(q('').matches.length, 4);
  assert.throws(() => q('x'.repeat(257)), /limited/);
  assert.throws(() => q('', { source: 'component:' }), /Unknown log source/);
  const big = { records: Array.from({ length: 1500 }, (_, i) => ({ source: 'daemon', line: `line ${i}` })) };
  const r = search.searchArchive(big, { text: 'line', source: '', caseSensitive: false });
  assert.equal(r.matches.length, 1000); assert.equal(r.truncated, true);
  assert.deepEqual(search.archiveSources(archive), ['component:app', 'daemon']);
  let log = [];
  for (let i = 0; i < 205; i++) log = search.recordAccess(log, { atMs: i, account: signer, authority: 'support', device: deviceId, index: i, kind: 'logs', outcome: 'opened' });
  assert.equal(log.length, 200); assert.equal(log[0].index, 204);
  // ?device= links select only a full object ID; anything else selects nothing.
  assert.equal(views.linkedDevice(deviceId.toUpperCase().replace('0X', '0x')), deviceId);
  for (const bad of [null, '', '0xd1', 'javascript:alert(1)', `${deviceId}"><script>`, `${deviceId}&x=1`, deviceId.slice(2), `0x${'g'.repeat(64)}`])
    assert.equal(views.linkedDevice(bad), null, String(bad));
  assert.equal(views.deviceHref('/data/', deviceId), `/data/?device=${deviceId}`);
  assert.equal(views.deviceHref('/services/', 'a&b=<c>'), '/services/?device=a%26b%3D%3Cc%3E');
  console.log('PASS: SF-084 dashboard views: verified/unavailable service views, alert drill-down, strict rollout progress/decisions, bounded literal log search, session access record and strict device links');
})().catch(error => { console.error(error); process.exitCode = 1; });
