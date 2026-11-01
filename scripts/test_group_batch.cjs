// SF-171 bulk group membership: selection, review plan, batch limits,
// architecture mismatches, transaction shape and partial-failure outcomes.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const manifest = require('../manifests/fixture.localnet.json'), protocol = require('../interfaces/fleet-v1.json');
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const load = loader(), batch = load('lib/group-batch.ts');
const { bcs } = appRequire('@mysten/sui/bcs');
const { normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');

// Batch support is detected from the manifest's reviewed type origin.
assert.equal(batch.groupBatchSupported(manifest), false);
const withBatch = structuredClone(manifest);
for (const name of protocol.optional_type_origins.group_batch) withBatch.type_origins[name] = id('0x171');
assert.equal(batch.groupBatchSupported(withBatch), true);
assert.equal(batch.MAX_FLEET_CHANGES, 100, 'must match customer::MAX_GROUP_BATCH');

const device = (n, arch, extra = {}) => ({ objectId: id(`0xd${n}`), name: `dev-${n}`, chainName: `dev-${n}`, serial: `SN-${n}`, arch, meta: {}, deviceAddress: id('0x1'), registeredAt: 0, managed: true, ...extra });
const devices = [device(1, 'aarch64'), device(2, 'aarch64'), device(3, 'x86_64'), device(4, 'aarch64', { name: 'twin', serial: undefined }), device(5, 'aarch64', { name: 'twin', serial: 'SN-5' })];
const canary = { objectId: id('0xc1'), name: 'Canary', description: '', admin: id('0xf'), deviceIds: [devices[0].objectId], unindexed: 0 };
const site = { objectId: id('0xc2'), name: 'Site B', description: '', admin: id('0xf'), deviceIds: [], unindexed: 0 };
const fleet = { id: id('0xf'), customerId: id('0xe'), devices: devices.map(d => d.objectId), groups: [canary.objectId, site.objectId] };

// Filter and search.
assert.deepEqual(batch.filterDevices(devices, 'x86').map(d => d.name), ['dev-3']);
assert.deepEqual(batch.filterDevices(devices, 'sn-2').map(d => d.name), ['dev-2']);
assert.deepEqual(batch.filterDevices(devices, 'canary', [canary]).map(d => d.name), ['dev-1']);
assert.equal(batch.filterDevices(devices, '  ').length, devices.length);

// Pasted lists: exact serial, name or object ID; unknown and ambiguous entries are reported, never guessed.
const pasted = batch.resolveDeviceList(`SN-1\nsn-3, dev-2;${devices[4].objectId}\nmissing\ntwin\nSN-1`, devices);
assert.deepEqual(pasted.matched.map(d => d.name).sort(), ['dev-1', 'dev-2', 'dev-3', 'twin']);
assert.deepEqual(pasted.unmatched, ['missing']);
assert.deepEqual(pasted.ambiguous, ['twin']);

// Review plan: members are skipped; mismatched architectures are excluded unless included explicitly.
const all = devices.map(d => d.objectId);
const plan = batch.planMembership('add', all, [canary.objectId, site.objectId], devices, [canary, site], { managed: true });
assert.deepEqual(plan.skipped.map(s => [s.deviceId, s.reason.split(' ')[0]]), [[devices[0].objectId, 'already'], [devices[2].objectId, 'architecture']]);
assert.deepEqual(plan.mismatches, [{ groupId: canary.objectId, deviceId: devices[2].objectId, arch: 'x86_64', groupArchs: ['aarch64'] }]);
assert.equal(plan.changes.length, 3 + 5, 'an empty group has no architecture to mismatch');
assert.equal(plan.transactions.length, 1);
const mixed = batch.planMembership('add', all, [canary.objectId], devices, [canary, site], { managed: true, includeMismatched: true });
assert.equal(mixed.changes.length, 4);
assert.equal(mixed.mismatches.length, 1);
const removal = batch.planMembership('remove', all, [canary.objectId], devices, [canary, site], { managed: true });
assert.deepEqual(removal.changes.map(c => c.deviceId), [devices[0].objectId]);
assert.equal(removal.skipped.length, 4);
assert.throws(() => batch.planMembership('add', all, [id('0xbad')], devices, [canary], { managed: true }), /group/);
assert.throws(() => batch.planMembership('add', [id('0xbad')], [site.objectId], devices, [site], { managed: true }), /device/);

// Batch limits: 100 changes per managed transaction, 25 per wallet transaction,
// and a wallet transaction never repeats a device (one inbox creation each).
const many = Array.from({ length: 250 }, (_, i) => device(100 + i, i % 2 ? 'x86_64' : 'aarch64'));
const big = batch.planMembership('add', many.map(d => d.objectId), [site.objectId], many, [site], { managed: true });
assert.deepEqual(big.transactions.map(t => t.length), [100, 100, 50]);
const wallet = batch.planMembership('add', many.slice(0, 60).map(d => d.objectId), [site.objectId], many, [site], { managed: false });
assert.deepEqual(wallet.transactions.map(t => t.length), [25, 25, 10]);
const twoGroups = batch.planMembership('add', many.slice(0, 3).map(d => d.objectId), [site.objectId, id('0xc3')], many,
  [site, { ...site, objectId: id('0xc3') }], { managed: false });
for (const tx of twoGroups.transactions) assert.equal(new Set(tx.map(c => c.deviceId)).size, tx.length);
assert.throws(() => batch.buildMembershipTx('add', big.changes.slice(0, 101), many, fleet, undefined, true), /bounds/);
assert.throws(() => batch.buildMembershipTx('add', wallet.changes.slice(0, 26), many, undefined, undefined, false), /bounds/);
assert.throws(() => batch.buildMembershipTx('add', [], many, fleet, undefined, true), /bounds/);

// Managed fleets with batch support: one bounded call per group and architecture,
// each pinned to the reviewed architecture.
const fns = tx => tx.getData().commands.map(c => `${c.MoveCall.module}::${c.MoveCall.function}`);
const pure = (tx, arg) => tx.getData().inputs[arg.Input].Pure.bytes;
const built = batch.buildMembershipTx('add', big.transactions[1], many, fleet, undefined, true);
assert.deepEqual(fns(built), ['customer::add_many_to_group', 'customer::add_many_to_group']);
const archs = built.getData().commands.map(c => bcs.option(bcs.string()).parse(Buffer.from(pure(built, c.MoveCall.arguments[5]), 'base64')));
assert.deepEqual(archs, ['aarch64', 'x86_64']);
const counts = built.getData().commands.map(c => bcs.vector(bcs.Address).parse(Buffer.from(pure(built, c.MoveCall.arguments[4]), 'base64')).length);
assert.deepEqual(counts, [25, 75]);
const removed = batch.buildMembershipTx('remove', big.transactions[1], many, fleet, undefined, true);
assert.deepEqual(fns(removed), ['customer::remove_many_from_group'], 'removal is not split by architecture');
// Older managed packages: one call per change, same bound.
assert.deepEqual([...new Set(fns(batch.buildMembershipTx('add', plan.transactions[0], devices, fleet, undefined, false)))], ['customer::add_to_group']);
// Wallet-administered groups borrow activated devices.
const legacy = batch.buildMembershipTx('add', [{ groupId: site.objectId, deviceId: devices[0].objectId, arch: 'aarch64' }],
  [{ ...devices[0], ownerCapId: id('0xca') }], undefined, undefined, false);
assert.deepEqual(fns(legacy), ['device_registry::borrow_device', 'device_group::add_device', 'device_registry::return_device']);

// Partial failure: transactions are atomic. Successful ones applied everything,
// definite failures nothing; later ones were never sent. A membership snapshot
// never decides an ambiguous submission.
const three = batch.planMembership('add', many.slice(0, 250).map(d => d.objectId), [site.objectId], many, [site], { managed: true });
const ok = batch.membershipOutcome(three, [0, 1, 2].map(index => ({ index, status: 'succeeded', digest: `D${index}` })));
assert.equal(ok.changed.length, 250);
const counts4 = o => [o.changed.length, o.unchanged.length, o.unknown.length, o.notAttempted.length];

// Submission status: refusal before the wallet, wallet rejection or failed
// effects are definite; a wallet transport error or confirmation timeout is not.
assert.equal(batch.failureStatus(false, false), 'failed', 'refused before signing');
assert.equal(batch.failureStatus(true, true), 'failed', 'wallet rejection or failed effects');
assert.equal(batch.failureStatus(true, false), 'ambiguous', 'transport error or confirmation timeout');

// Definite wallet rejection: nothing applied.
const rejected = [{ index: 0, status: 'succeeded', digest: 'D0' }, { index: 1, status: 'failed', error: 'User rejected the request' }];
assert.deepEqual(counts4(batch.membershipOutcome(three, rejected)), [100, 100, 0, 50]);

(async () => {
  const never = async () => { throw Error('lookup must not be called'); };
  // Transport error without a digest: unknown, and no digest to reconcile.
  const transport = [{ index: 0, status: 'succeeded', digest: 'D0' }, { index: 1, status: 'ambiguous', error: 'wallet disconnected' }];
  const noDigest = await batch.reconcileResults(transport, never);
  assert.deepEqual(counts4(batch.membershipOutcome(three, noDigest)), [100, 0, 100, 50]);

  // Confirmation timeout with a digest: unknown while the digest is not found
  // (or the node errors), then changed once its effects show late success.
  const one = batch.planMembership('add', [devices[1].objectId], [site.objectId], devices, [site], { managed: true });
  const timeout = [{ index: 0, status: 'ambiguous', digest: 'submitted-digest', error: 'confirmation timeout' }];
  assert.deepEqual(counts4(batch.membershipOutcome(one, timeout)), [0, 0, 1, 0], 'regression: was reported unchanged');
  const pending = await batch.reconcileResults(timeout, async () => undefined);
  assert.deepEqual(counts4(batch.membershipOutcome(one, pending)), [0, 0, 1, 0]);
  const unreachable = await batch.reconcileResults(timeout, async () => { throw Error('node unavailable'); });
  assert.deepEqual(counts4(batch.membershipOutcome(one, unreachable)), [0, 0, 1, 0]);
  const late = await batch.reconcileResults(pending, async digest => digest === 'submitted-digest' ? 'succeeded' : undefined);
  assert.deepEqual(late[0], { ...timeout[0], status: 'succeeded' }, 'digest and error are kept for the report');
  assert.deepEqual(batch.membershipOutcome(one, late).changed.map(c => c.deviceId), [devices[1].objectId]);

  // Failed effects found for the digest: definitely unchanged.
  const failedEffects = await batch.reconcileResults(timeout, async () => 'failed');
  assert.deepEqual(counts4(batch.membershipOutcome(one, failedEffects)), [0, 1, 0, 0]);

  // Settled results are not looked up again.
  assert.deepEqual(await batch.reconcileResults([...rejected, ...late], never), [...rejected, ...late]);
  const removalOutcome = batch.membershipOutcome(removal, [{ index: 0, status: 'ambiguous', error: 'timeout' }]);
  assert.deepEqual(removalOutcome.unknown.map(c => c.deviceId), [devices[0].objectId]);

  // Run summary: success needs every planned transaction confirmed, not just
  // every attempted one.
  const lateFirst = [{ index: 0, status: 'succeeded', digest: 'late', error: 'timeout' }];
  assert.deepEqual(batch.runSummary(three, lateFirst), { complete: false, confirmed: 1, unsent: 2, failure: undefined });
  assert.equal(batch.runSummary(three, [0, 1, 2].map(index => ({ index, status: 'succeeded' }))).complete, true);
  assert.equal(batch.runSummary(three, rejected).failure, rejected[1]);

  // Component: the real dialog, driven through review, apply, settle and Check
  // again with a stateful hook runtime; wallet and node calls are injected.
  await dialogRegressions(many, site, fleet);
  console.log('PASS: bulk group selection, review plan, batch limits, architecture mismatches, transaction shape and partial-failure outcomes');
})().catch(err => { console.error(err); process.exit(1); });

/**
 * Render BulkGroupMembership with a minimal hook runtime (state persists by
 * call order) and return its text, buttons and inputs. `wallet(i, hooks)` plays
 * transaction i; `node.lookup(digest)` answers digest effects lookups.
 */
async function dialogRegressions(many, site, fleet) {
  let slots = [], cursor = 0;
  const react = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = { value: initial };
      const slot = slots[i];
      return [slot.value, (next) => { slot.value = typeof next === 'function' ? next(slot.value) : next; }];
    },
    useMemo: (fn) => (cursor++, fn()),
  };
  const env = {};
  const component = loader({
    react,
    '@mysten/dapp-kit-react': { useCurrentClient: () => ({ async getTransaction({ digest }) { return env.lookup(digest); } }) },
    '@/hooks/useFleetTransaction': { useFleetTransaction: () => ({ async mutateAsync(options) { return env.wallet(env.calls++, options); } }) },
  })('components/BulkGroupMembership.tsx');
  const flush = () => new Promise(resolve => setImmediate(resolve));
  function render(props) {
    cursor = 0;
    const buttons = {}, inputs = [], text = [];
    (function walk(node) {
      if (node == null || typeof node === 'boolean') return;
      if (Array.isArray(node)) return node.forEach(walk);
      if (typeof node !== 'object') return void text.push(String(node));
      if (typeof node.type === 'function') return walk(node.type(node.props));
      if (node.type === 'button') buttons[[node.props.children].flat().join('')] = node.props;
      if (node.type === 'input') inputs.push(node.props);
      walk(node.props?.children);
    })(component.BulkGroupMembership(props));
    return { buttons, inputs, text: text.join('') };
  }
  const props = { devices: many, groups: [site], selected: many.map(d => d.objectId), fleet, reloadGroups: async () => {}, onClose() {} };
  const found = (status) => ({ Transaction: { status: { success: status === 'succeeded' } } });
  async function run(wallet, lookup) {
    slots = []; Object.assign(env, { calls: 0, wallet, lookup });
    render(props).inputs.find(i => i.type === 'checkbox' && !i.checked).onChange({ target: { checked: true } });
    const approve = render(props).buttons['Approve 3 transaction(s)'];
    assert.ok(approve, 'review plans three transactions');
    approve.onClick();
    for (let i = 0; i < 20; i++) await flush();
    return render(props);
  }
  const confirm = (digest) => ({ onProgress, onSubmitted }) => { onProgress('wallet'); onProgress('confirming'); onSubmitted(digest); return { digest }; };
  const timeout = (digest) => ({ onProgress, onSubmitted }) => { onProgress('wallet'); onProgress('confirming'); onSubmitted(digest); throw Error('confirmation timeout'); };

  // Every transaction confirmed: complete.
  const all = await run((i, o) => confirm(`D${i}`)(o), async () => { throw Error('lookup must not be called'); });
  assert.match(all.text, /All 3 transaction\(s\) succeeded\./);
  assert.doesNotMatch(all.text, /Nothing is retried/);

  // Regression: the first batch times out, then settles as succeeded at once.
  // Two batches were never sent, so this is not a full success.
  const first = await run((i, o) => (i === 0 ? timeout('D0') : confirm(`D${i}`))(o), async () => found('succeeded'));
  assert.equal(env.calls, 1, 'the run stops at the unconfirmed transaction');
  assert.doesNotMatch(first.text, /All 3 transaction/);
  assert.match(first.text, /1 of 3 transaction\(s\) succeeded; transaction 1 was confirmed late \(confirmation timeout\)\. The remaining 2 transaction\(s\) were not sent\./);
  assert.match(first.text, /Not attempted \(150\)/);
  assert.match(first.text, /Review again to plan only the remaining changes\./);

  // Regression: the middle batch times out and is not found yet; Check again
  // then finds its successful effects. The last batch is still unsent.
  let effects;
  const middle = await run((i, o) => (i === 1 ? timeout('D1') : confirm(`D${i}`))(o), async () => effects);
  assert.equal(env.calls, 2);
  assert.match(middle.text, /Transaction 2 of 3 was not confirmed: confirmation timeout\. Later transactions were not sent\./);
  assert.match(middle.text, /Unknown \(100\)/);
  assert.match(middle.text, /Resolve the unknown transaction first/);
  effects = found('succeeded');
  middle.buttons['Check again'].onClick();
  for (let i = 0; i < 20; i++) await flush();
  const rechecked = render(props);
  assert.doesNotMatch(rechecked.text, /All 3 transaction/);
  assert.match(rechecked.text, /2 of 3 transaction\(s\) succeeded; transaction 2 was confirmed late \(confirmation timeout\)\. The remaining 1 transaction\(s\) were not sent\./);
  assert.match(rechecked.text, /Not attempted \(50\)/);
  assert.doesNotMatch(rechecked.text, /Unknown \(/);
  assert.match(rechecked.text, /Review again to plan only the remaining changes\./);
}
