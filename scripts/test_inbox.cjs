// SF-125 dApp builders: announcements in deployment PTBs, inbox-aware group
// membership, the member-index migration and indexed member reads. Offline.
const fs = require('node:fs'), assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { bcs } = appRequire('@mysten/sui/bcs');
const { deriveDynamicFieldID, normalizeSuiAddress: n } = appRequire('@mysten/sui/utils');
const { ObjectError } = appRequire('@mysten/sui/client');
const base = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
const protocol = JSON.parse(fs.readFileSync('interfaces/fleet-v1.json'));
const fns = tx => tx.getData().commands.map(c => c.MoveCall ? `${c.MoveCall.module}::${c.MoveCall.function}` : Object.keys(c)[0]);
const DIGEST = 'a'.repeat(64);

// Without inbox origins every builder keeps its previous calls.
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(base);
{
  const load = loader(), sui = load('lib/sui.ts'), inbox = load('lib/fleet-inbox.ts');
  assert.equal(inbox.inboxSupported(), false);
  assert.equal(inbox.announcementsFor([], [], '0xad', ['0x10'], ['0xde'], new Set()), undefined);
  assert.deepEqual(fns(sui.buildCreateDeploymentTx('app', '1', 'blob', DIGEST, DIGEST, ['0x10'], [])), ['deployment_manager::create_deployment_verified']);
  assert.deepEqual(fns(sui.buildAddDeviceToGroupTx('0x10', { objectId: '0xde', ownerCapId: '0xc1' }, false)),
    ['device_registry::borrow_device', 'device_group::add_device', 'device_registry::return_device']);
  assert.deepEqual(fns(sui.buildRemoveDeviceFromGroupTx('0x10', '0xde', true)), ['device_group::remove_device']);
  assert.throws(() => inbox.buildIndexGroupTx('0x10', []), /no group index/);
}

const m = structuredClone(base);
const ORIGIN = n('0x125');
for (const name of protocol.optional_type_origins.fleet_inbox) m.type_origins[name] = ORIGIN;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), sui = load('lib/sui.ts'), inbox = load('lib/fleet-inbox.ts'), rollout = load('lib/rollout.ts');
(async () => {
  assert.equal(inbox.inboxSupported(), true);
  // The derived address the daemon and Move compute (DerivedObjectKey<InboxKey>).
  const device = n('0xde');
  assert.equal(inbox.inboxId(device), deriveDynamicFieldID(m.shared_objects.registry.id,
    `0x2::derived_object::DerivedObjectKey<${ORIGIN}::fleet_inbox::InboxKey>`, bcs.Address.serialize(device).toBytes()));
  assert.notEqual(inbox.inboxId(device), inbox.inboxId(n('0xdf')));

  // Only groups the sender administers and devices it holds caps for.
  const groups = [{ objectId: '0x10', admin: '0xad' }, { objectId: '0x11', admin: '0xbad' }];
  const devices = [{ objectId: '0xde', ownerCapId: '0xc1' }, { objectId: '0xdf', ownerCapId: '0xc2' }, { objectId: '0xe0' }];
  const targets = inbox.announcementsFor(groups, devices, '0xad', ['0x10', '0x11'], ['0xde', '0xdf', '0xe0'], new Set(['0xde']));
  assert.deepEqual(targets, { groups: ['0x10'], devices: [
    { deviceId: '0xde', ownerCapId: '0xc1', exists: true }, { deviceId: '0xdf', ownerCapId: '0xc2', exists: false }] });
  const deploy = sui.buildCreateDeploymentTx('app', '1', 'blob', DIGEST, DIGEST, ['0x10', '0x11'], ['0xde', '0xdf', '0xe0'], true, targets);
  assert.deepEqual(fns(deploy), ['deployment_manager::new_deployment_verified', 'deployment_manager::set_control',
    'fleet_inbox::announce_group', 'fleet_inbox::announce_device', 'fleet_inbox::new_inbox', 'fleet_inbox::announce_device',
    'fleet_inbox::share_inbox', 'deployment_manager::share_deployment']);
  // The record is announced before it is shared, and to the derived inbox.
  const data = deploy.getData();
  const announceDevice = data.commands[3].MoveCall;
  assert.equal(announceDevice.arguments[3].$kind === 'NestedResult' ? announceDevice.arguments[3].NestedResult[0] : announceDevice.arguments[3].Result, 0);
  const inboxInput = data.inputs[announceDevice.arguments[0].Input];
  assert.equal(n(inboxInput.UnresolvedObject.objectId), inbox.inboxId('0xde'));

  // Membership lists the group in the device's inbox, creating it if needed.
  assert.deepEqual(fns(sui.buildAddDeviceToGroupTx('0x10', { objectId: '0xde', ownerCapId: '0xc1' }, false)),
    ['fleet_inbox::new_inbox', 'device_registry::borrow_device', 'fleet_inbox::add_member', 'device_registry::return_device', 'fleet_inbox::share_inbox']);
  assert.deepEqual(fns(sui.buildAddDeviceToGroupTx('0x10', { objectId: '0xde', ownerCapId: '0xc1' }, true)),
    ['device_registry::borrow_device', 'fleet_inbox::add_member', 'device_registry::return_device']);
  assert.deepEqual(fns(sui.buildAddDeviceToGroupTx('0x10', { objectId: '0xe0' })), ['device_group::add_device']);
  assert.deepEqual(fns(sui.buildRemoveDeviceFromGroupTx('0x10', '0xde', true)), ['fleet_inbox::remove_member']);
  assert.deepEqual(fns(sui.buildRemoveDeviceFromGroupTx('0x10', '0xe0', false)), ['device_group::remove_device']);

  // Index migration: one bounded batch plus inbox listings.
  const index = inbox.buildIndexGroupTx('0x10', [{ deviceId: '0xde', ownerCapId: '0xc1', exists: true }, { deviceId: '0xdf', ownerCapId: '0xc2', exists: false }]);
  assert.deepEqual(fns(index), ['device_group::index_members', 'fleet_inbox::follow_group', 'fleet_inbox::new_inbox', 'fleet_inbox::follow_group', 'fleet_inbox::share_inbox']);

  // Rollouts announce to members' existing inboxes only.
  const input = { component: 'app', version: '1', recipe: 'blob', artifactSha256: DIGEST, recipeSha256: DIGEST, cohorts: [['0xde'], ['0xdf']],
    concurrency: 1, rateCount: 1, rateWindowMs: 1000, bakeMs: 100, minimumSamples: 1, failureBps: 5000, offlineTimeoutMs: 2000 };
  assert.deepEqual(fns(rollout.buildRolloutTx(input, { groups: [], devices: [{ deviceId: '0xde', ownerCapId: '0xc1', exists: true }] })),
    ['deployment_manager::rollout_policy', 'deployment_manager::new_rollout_verified', 'fleet_inbox::announce_device', 'deployment_manager::share_deployment']);

  // Inbox existence: missing is absent, a wrong type fails closed.
  const inboxType = `${ORIGIN}::fleet_inbox::DeviceInbox`;
  const found = await inbox.existingInboxes({ getObjects: async ({ objectIds }) => ({ objects: objectIds.map((id, i) =>
    i === 0 ? { objectId: id, type: inboxType } : new ObjectError('notExists', 'absent', { reason: 'notFound' })) }) }, ['0xde', '0xdf']);
  assert.deepEqual([...found], ['0xde']);
  await assert.rejects(inbox.existingInboxes({ getObjects: async ({ objectIds }) => ({ objects: objectIds.map(id => ({ objectId: id, type: '0x2::coin::Coin' })) }) }, ['0xde']), /Unexpected object/);

  // Indexed members are MemberKey fields, read across pages.
  const key = (id) => ({ name: { type: `${ORIGIN}::device_group::MemberKey`, bcs: bcs.Address.serialize(id).toBytes() } });
  const pages = [{ hasNextPage: true, cursor: 'c1', dynamicFields: [key('0xde'), { name: { type: `${ORIGIN}::fleet_inbox::HeadKey`, bcs: new Uint8Array([0]) } }] },
    { hasNextPage: false, cursor: null, dynamicFields: [key('0xdf')] }];
  let calls = 0;
  assert.deepEqual(await inbox.indexedMembers({ listDynamicFields: async () => pages[calls++] }, '0x10'), [n('0xde'), n('0xdf')]);
  await assert.rejects(inbox.indexedMembers({ listDynamicFields: async () => ({ hasNextPage: true, cursor: null, dynamicFields: [] }) }, '0x10'), /no progress/);
  console.log('PASS: inbox announcements, derived inbox IDs, inbox-aware membership, index migration, rollout announcements and indexed member reads');
})().catch(e => { console.error(e); process.exitCode = 1; });
