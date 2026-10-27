const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(require('../manifests/fixture.localnet.json'));
process.env.NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL = 'http://must-never-be-used.invalid';
const load = loader(), { planOperation } = load('lib/fleet-ops.ts');
const { normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
const owner = id('0xa'), group = { objectId: id('0xb'), admin: owner, deviceIds: [] };
const devices = [{ objectId: id('0xd1'), ownerCapId: id('0xc1') }, { objectId: id('0xd2') }];
const add = { command: 'add-devices', sender: owner, group: group.objectId, devices: devices.map(d => d.objectId) };
const data = planOperation(add, devices, [group], new Set()).getData();
assert.deepEqual(data.commands.map(c => c.MoveCall.function), ['borrow_device', 'add_device', 'return_device', 'add_device']);
const calls = data.commands.map(c => c.MoveCall);
assert.deepEqual(calls[1].arguments[1], { NestedResult: [0, 0], $kind: 'NestedResult' });
assert.deepEqual(calls[2].arguments.slice(1), [{ NestedResult: [0, 0], $kind: 'NestedResult' }, { NestedResult: [0, 1], $kind: 'NestedResult' }]);
const populated = { ...group, deviceIds: devices.map(d => d.objectId) };
assert.deepEqual(planOperation({ ...add, command: 'remove-devices' }, devices, [populated], new Set()).getData().commands.map(c => c.MoveCall.function), ['remove_device', 'remove_device']);
for (const [request, owned, groups] of [
  [{ ...add, command: 'unknown' }, devices, [group]],
  [add, [], [group]], [add, devices, []], [add, devices, [populated]],
  [{ ...add, devices: [id('0xd1'), id('0xd1')] }, devices, [group]],
  [{ ...add, devices: [] }, devices, [group]],
]) assert.throws(() => planOperation(request, owned, groups, new Set()));
const deployment = { command: 'create-deployment', sender: owner, component: 'app', version: '1', recipeBlob: 'blob',
  artifactSha256: 'a'.repeat(64), recipeSha256: 'b'.repeat(64), targetDevices: [id('0xd1')], targetGroups: [group.objectId] };
assert.throws(() => planOperation(deployment, devices, [group], new Set()), /authority/);
const tx = planOperation(deployment, devices, [group], new Set([id('0xd1')]));
assert.equal(tx.getData().commands[0].MoveCall.function, 'create_deployment_verified');
assert.throws(() => planOperation({ ...deployment, recipeSha256: '' }, devices, [group], new Set([id('0xd1')])), /digests/);
// With inbox origins (SF-125) the same intents list/unlist groups in device
// inboxes (creating a missing one before the custody loan) and announce
// deployments to group heads and operated device inboxes.
{
  const manifest = require('../manifests/fixture.localnet.json'), protocol = require('../interfaces/fleet-v1.json');
  const withInbox = structuredClone(manifest);
  for (const name of protocol.optional_type_origins.fleet_inbox) withInbox.type_origins[name] = id('0x125');
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(withInbox);
  const inboxLoad = loader(), plan = inboxLoad('lib/fleet-ops.ts').planOperation, inboxId = inboxLoad('lib/fleet-inbox.ts').inboxId;
  const fns = tx => tx.getData().commands.map(c => `${c.MoveCall.module}::${c.MoveCall.function}`);
  const fresh = plan(add, devices, [group], new Set(), new Set());
  assert.deepEqual(fns(fresh), ['fleet_inbox::new_inbox', 'device_registry::borrow_device', 'fleet_inbox::add_member',
    'device_registry::return_device', 'fleet_inbox::share_inbox', 'device_group::add_device']);
  const existing = plan(add, devices, [group], new Set(), new Set([id('0xd1')]));
  assert.deepEqual(fns(existing), ['device_registry::borrow_device', 'fleet_inbox::add_member', 'device_registry::return_device', 'device_group::add_device']);
  assert.equal(existing.getData().inputs.find(i => i.UnresolvedObject?.objectId === inboxId(id('0xd1'))) !== undefined, true);
  assert.deepEqual(fns(plan({ ...add, command: 'remove-devices' }, devices, [populated], new Set(), new Set([id('0xd1')]))),
    ['fleet_inbox::remove_member', 'device_group::remove_device']);
  assert.deepEqual(fns(plan(deployment, devices, [group], new Set([id('0xd1')]), new Set())),
    ['deployment_manager::new_deployment_verified', 'fleet_inbox::announce_group', 'fleet_inbox::new_inbox',
      'fleet_inbox::announce_device', 'fleet_inbox::share_inbox', 'deployment_manager::share_deployment']);
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
}
assert.equal(load('lib/telemetry.ts').TELEMETRY_URL, '', 'a legacy URL must not configure product reads');
(async () => {
  const result = await load('lib/telemetry.ts').fetchFleetHealth({}, [id('0xd1')], owner, undefined, '', () => { throw Error('HTTP must not be used'); });
  assert.equal(result[id('0xd1')].status, 'unavailable');
  assert.match(result[id('0xd1')].reasons[0], /not set up.*configure fleet data/);
  console.log('PASS: serverless default ignores legacy URL; locally planned bulk custody/return, membership, inbox listing/announcement, authority, digest and active-identity checks');
})().catch(error => { console.error(error); process.exitCode = 1; });
