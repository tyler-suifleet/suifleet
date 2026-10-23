// SF-129 reclaim planning, transaction building and previews over exact
// on-chain BCS layouts. Simulation results are injected; the localnet
// lifecycle test (scripts/test_reclaim_localnet.py) executes the real calls.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const manifest = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../manifests/fixture.localnet.json')));
manifest.chain_identifier = '4c78adac';
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
const { normalizeSuiAddress: id, toBase58 } = appRequire('@mysten/sui/utils');
const { bcs } = appRequire('@mysten/sui/bcs');
const { ObjectError } = appRequire('@mysten/sui/client');
const load = loader();
const reclaim = load('lib/storage-reclaim.ts'), bulk = load('lib/fleet-bulk.ts'), data = load('lib/fleet-data.ts');
const protocol = load('lib/fleet-protocol.ts'), retention = load('lib/retention.ts');

const origin = id('0x81'), storageOrigin = id('0x5a'), system = id('0x5e');
const config = { schema: 1, chainIdentifier: '4c78adac', registryId: id(manifest.shared_objects.registry.id), packageId: id('0x82'),
  typeOrigin: origin, fleetId: id('0xf1'), budgetId: id('0xb1'), storageId: id('0x51') };
const device = id('0xd1'), writerId = id('0xe1'), other = id('0xd2');
const missing = () => new ObjectError('missing', 'not found', { reason: 'notFound' });
const anchor = (seq, blob, end) => ({ seq: String(seq), kind: 0, blob_object_id: blob, blob_id: String(seq), ciphertext_sha256: Array(32).fill(seq),
  signer: id('0x5160'), at_ms: String(seq * 1000), end_epoch: end, key_epoch: '0' });
const record = (end, owner = device) => ({ device_id: owner, blob_id: '1', size: '10', encoded_size: '100', end_epoch: end, uploaded_at_ms: '1' });

/** A fleet with one writer. `anchors` maps index → anchor; absent indices are pruned. */
function world({ anchors, records, count = 6 }) {
  const fleet = { id: config.fleetId, version: '1', budget_id: config.budgetId, storage_id: config.storageId, registry_id: config.registryId,
    chain_identifier: config.chainIdentifier, key_epoch: '0', support: [], health_interval_ms: '86400000', batch_interval_ms: '86400000', emergency_interval_ms: '300000', device_count: '1' };
  const writer = { id: writerId, version: '1', fleet_id: config.fleetId, device_id: device, account_id: id('0xa1'), health_seq: '0', health_at_ms: '0',
    message: [], signature: [], batch_seq: '99', batch_at_ms: null, emergency_at_ms: null, anchor_count: String(count) };
  const reads = [];
  return { reads,
    ledgerService: { async getServiceInfo() { return { response: { chainId: toBase58(Buffer.from(config.chainIdentifier + '00'.repeat(28), 'hex')) } }; } },
    async getObject({ objectId }) {
      const isFleet = objectId === config.fleetId;
      return { object: { owner: { $kind: 'Shared' }, type: `${origin}::fleet_data::${isFleet ? 'FleetData' : 'DeviceData'}`,
        content: (isFleet ? data.FleetDataBcs : data.DeviceDataBcs).serialize(isFleet ? fleet : writer).toBytes() } };
    },
    async getDynamicField({ parentId, name }) {
      if (parentId === config.fleetId) {
        if (id(bcs.Address.parse(name.bcs)) !== device) throw missing();
        return { dynamicField: { value: { type: '0x2::object::ID', bcs: bcs.Address.serialize(writerId).toBytes() } } };
      }
      if (parentId === writerId) {
        const index = Number(bcs.u64().parse(name.bcs)); reads.push(index);
        if (!anchors[index]) throw missing();
        return { dynamicField: { value: { type: `${origin}::fleet_data::Anchor`, bcs: data.AnchorBcs.serialize(anchors[index]).toBytes() } } };
      }
      assert.equal(parentId, config.storageId);
      assert.equal(name.type, `${storageOrigin}::fleet_storage::RecordKey`);
      const blob = id(bcs.Address.parse(name.bcs));
      if (!records[blob]) throw missing();
      return { dynamicField: { value: { type: `${storageOrigin}::fleet_storage::BlobRecord`, bcs: retention.RecordBcs.serialize(records[blob]).toBytes() } } };
    },
    async listDynamicFields({ parentId }) {
      assert.equal(parentId, config.storageId);
      return { hasNextPage: false, dynamicFields: Object.entries(records).map(([blob, r]) => ({
        name: { type: `${storageOrigin}::fleet_storage::RecordKey`, bcs: bcs.Address.serialize(blob).toBytes() },
        value: { bcs: retention.RecordBcs.serialize(r).toBytes() } })) };
    },
  };
}
const calls = (tx) => tx.getData().commands.map((c) => c.MoveCall && `${c.MoveCall.module}::${c.MoveCall.function}`);

(async () => {
  // Readers: pruned indices are skipped, exact reads never return a neighbour.
  const b = (n) => id('0xb0' + n);
  const history = { 1: anchor(2, b(1), 5), 3: anchor(4, b(3), 9), 4: anchor(5, b(4), 12) };
  let chain = world({ anchors: history, records: {} });
  const page = await bulk.readAnchorEntries(chain, config, device, 0, 2);
  assert.deepEqual(page.entries.map((e) => e.index), [1, 3]);
  assert.equal(page.next, 4);
  const rest = await bulk.readAnchorEntries(chain, config, device, page.next, 64);
  assert.deepEqual(rest.entries.map((e) => e.index), [4]);
  assert.equal(rest.next, 6);
  assert.deepEqual((await bulk.readAnchors(chain, config, device, 0, 64)).map((a) => a.seq), ['2', '4', '5']);
  assert.equal(await bulk.readAnchorAt(chain, config, device, 0), undefined, 'pruned index must not resolve to the next archive');
  assert.equal((await bulk.readAnchorAt(chain, config, device, 3)).seq, '4');
  assert.equal(await bulk.readAnchorAt(chain, config, device, 6), undefined);
  // Pruned gaps are bounded per page; callers continue from `next`.
  chain = world({ anchors: { 400: anchor(9, b(9), 1) }, records: {}, count: 401 });
  const gap = await bulk.readAnchorEntries(chain, config, device, 0, 1);
  assert.deepEqual(gap.entries, []);
  assert.equal(gap.next, 257);
  assert.deepEqual((await bulk.readAnchorEntries(chain, config, device, gap.next, 1)).entries.map((e) => e.index), [400]);
  // Sequence order still binds the remaining history.
  chain = world({ anchors: { 0: anchor(5, b(1), 1), 2: anchor(4, b(2), 1) }, records: {} });
  await assert.rejects(bulk.readAnchorEntries(chain, config, device, 0, 8), /sequence mismatch/);

  // Archive plan at Walrus epoch 10.
  const held = b(1), extended = b(2), released = b(3), releasedLate = b(4), loose = b(5), ownerBlob = b(6), fresh = b(7);
  chain = world({
    anchors: { 0: anchor(1, held, 8), 1: anchor(2, extended, 8), 2: anchor(3, released, 10), 3: anchor(4, releasedLate, 11), 5: anchor(6, fresh, 30) },
    records: { [held]: record(8), [extended]: record(20), [loose]: record(9), [ownerBlob]: { ...record(3), device_id: null }, [fresh]: record(30) },
  });
  const plan = await reclaim.planArchiveReclaim(chain, config, storageOrigin, [device, other], 10);
  assert.deepEqual(plan.anchors.map((c) => [c.index, c.held, c.endEpoch]), [[0, true, 8], [2, false, 10]],
    'renewed blobs, late releases and fresh archives are kept');
  assert.deepEqual(plan.loose, [loose], 'owner release/recipe blobs and anchored blobs are not burned as loose archives');
  assert.equal(plan.scanned, 5);
  assert.equal(plan.truncated, false);
  const bounded = await reclaim.planArchiveReclaim(chain, config, storageOrigin, [device], 10, 2);
  assert.equal(bounded.truncated, true);
  await assert.rejects(reclaim.planArchiveReclaim(chain, config, storageOrigin, [device], -1), /Invalid Walrus epoch/);

  const built = reclaim.buildArchiveReclaimTx(config, id('0xca'), system, plan);
  assert.deepEqual(calls(built.tx), ['fleet_data::prune_expired_anchors', 'fleet_data::burn_expired_blobs']);
  assert.deepEqual([built.anchors, built.loose], [2, 1]);
  const prune = built.tx.getData().commands[0].MoveCall;
  assert.equal(id(prune.package), config.packageId);
  const inputs = built.tx.getData().inputs;
  const pureArg = (arg) => inputs[arg.Input].Pure.bytes;
  assert.deepEqual(bcs.vector(bcs.u64()).parse(Buffer.from(pureArg(prune.arguments[6]), 'base64')), ['0', '2']);
  // Large plans are split into 64-entry calls and capped per transaction.
  const many = { epoch: 10, anchors: Array.from({ length: 300 }, (_, index) => ({ ...plan.anchors[0], index })), loose: [loose], scanned: 300, truncated: false };
  const split = reclaim.buildArchiveReclaimTx(config, id('0xca'), system, many);
  assert.deepEqual([split.anchors, split.loose], [256, 0]);
  assert.deepEqual(calls(split.tx), Array(4).fill('fleet_data::prune_expired_anchors'));
  assert.throws(() => reclaim.buildArchiveReclaimTx(config, id('0xca'), system, { ...plan, anchors: [], loose: [] }), /Nothing to reclaim/);

  // Previews report what signing would return, and failures before signing.
  const gas = { computationCost: '1000000', storageCost: '988000', storageRebate: '21000000', nonRefundableStorageFee: '0' };
  // Previews simulate the built bytes (real gas coins); the build itself is stubbed here.
  const builtBytes = new Uint8Array([7]), simulated = [];
  built.tx.build = async () => builtBytes;
  const ok = await reclaim.previewReclaim({ async simulateTransaction({ transaction }) { simulated.push(transaction); return { $kind: 'Transaction', Transaction: { effects: { status: { success: true }, gasUsed: gas } } }; } }, built.tx, id('0xa0'));
  assert.equal(simulated[0], builtBytes, 'simulate the exact built bytes');
  assert.deepEqual([ok.ok, ok.netMist, ok.rebateMist], [true, BigInt(19012000), BigInt(21000000)]);
  const refused = await reclaim.previewReclaim({ async simulateTransaction() { return { $kind: 'FailedTransaction', FailedTransaction: { effects: { status: { success: false, error: { message: 'EUnexpired' } }, gasUsed: gas } } }; } }, built.tx, id('0xa0'));
  assert.equal(refused.ok, false); assert.match(refused.error, /EUnexpired/);
  // An abort during gas estimation is a refusal, not a thrown error.
  const aborted = reclaim.buildArchiveReclaimTx(config, id('0xca'), system, plan).tx;
  aborted.build = async () => { throw Error('Transaction resolution failed: MoveAbort ... abort code: 8'); };
  const estimate = await reclaim.previewReclaim({ async simulateTransaction() { throw Error('not reached'); } }, aborted, id('0xa0'));
  assert.deepEqual([estimate.ok, estimate.netMist], [false, BigInt(0)]); assert.match(estimate.error, /abort code: 8/);
  assert.equal(reclaim.formatReclaim(BigInt(19012000)), '0.019012000 SUI');
  assert.equal(reclaim.formatReclaim(BigInt(-5)), '-0.000000005 SUI');

  // Deployment records: finished, unmanaged or superseded only.
  const me = id('0xad'), g1 = id('0x61'), g2 = id('0x62'), gone = id('0x63');
  const control = { revision: '0', state: 0, not_before_ms: '0', expires_ms: '0', window_start_ms: '0', window_end_ms: '0', notice_ms: '0' };
  const deployment = (n, extra) => ({ deploymentId: id('0xd0' + n), schemaVersion: '1', componentName: 'app', version: String(n), recipeBlobId: 'r', artifactSha256: 'a',
    targetGroups: [g1], targetDevices: [], creator: me, timestampMs: 1, statuses: { [device]: 1 }, receiptsVerified: true, resolvedDeviceIds: [device], control, ...extra });
  const managed = new Set([id('0xd02'), id('0xd03'), id('0xd04')]);
  const desiredState = { [g1]: { revision: '2', components: { contents: [{ key: 'app', value: id('0xd03') }] }, updated_at: '1' } };
  const reads = {
    async getDynamicField({ parentId, name }) {
      if (name.type.endsWith('::deployment_manager::ManagedKey')) { if (!managed.has(id(parentId))) throw missing(); return { dynamicField: { value: { bcs: bcs.bool().serialize(true).toBytes() } } }; }
      if (!desiredState[parentId]) throw missing();
      return { dynamicField: { value: { type: `${manifest.type_origins['desired_state::DesiredState']}::desired_state::DesiredState`, bcs: protocol.desired_state_DesiredStateBcs.serialize(desiredState[parentId]).toBytes() } } };
    },
    async getObject({ objectId }) { if (objectId === gone) throw missing(); return { object: {} }; },
  };
  const records = [
    deployment(1),                                                    // unmanaged, every target reported
    deployment(2, { targetGroups: [g1, g2, g1] }),                    // managed, superseded everywhere
    deployment(3),                                                    // still desired by g1
    deployment(4, { targetGroups: [gone] }),                          // target group deleted
    deployment(5, { statuses: {} }),                                  // still in progress
    deployment(6, { statuses: {}, control: { ...control, state: 2 } }),
    deployment(7, { statuses: {}, control: { ...control, expires_ms: '50' } }),
    deployment(8, { creator: id('0xbe') }),                           // someone else's
  ];
  const planned = await reclaim.planDeploymentReclaim(reads, records, me, 100);
  assert.deepEqual(planned.map((r) => [r.deployment.version, r.eligible, r.reason]), [
    ['1', true, 'every target reported'], ['2', true, 'every target reported'],
    ['3', false, 'still desired by a group; publish a revision that supersedes it first'],
    ['4', false, 'a target group was deleted, so the on-chain check cannot run; delete it individually after review'],
    ['5', false, 'still in progress'], ['6', true, 'cancelled'], ['7', true, 'expired']]);
  assert.deepEqual(planned[0].groups, []);
  assert.deepEqual(planned[1].groups, [g1, g2]);
  const retire = reclaim.buildRetireDeploymentsTx(manifest.call_package_id, planned.slice(0, 2));
  assert.deepEqual(calls(retire), ['desired_state::begin_retirement', 'desired_state::retire',
    'desired_state::begin_retirement', 'desired_state::confirm_not_desired', 'desired_state::confirm_not_desired', 'desired_state::retire']);
  assert.throws(() => reclaim.buildRetireDeploymentsTx(manifest.call_package_id, planned.slice(2, 4)), /Nothing to reclaim/);
  assert.equal(reclaim.finishedReason(deployment(9, { receiptsVerified: false }), 0), undefined, 'unverified receipts never count as finished');
  console.log('PASS: SF-129 pruned-index readers, archive/deployment reclaim plans, batched transactions and signed previews');
})().catch((error) => { console.error(error); process.exitCode = 1; });
