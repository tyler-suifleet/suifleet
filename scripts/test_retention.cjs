// Real SDK builders + independent BCS layout comparison across runtime and UI.
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const { Transaction } = appRequire('@mysten/sui/transactions');
const { Retention, Renewal, Record, Budget, Account } = require('./retention_transport.cjs');
const r = loader()('lib/retention.ts');
const generated = loader()('lib/fleet-protocol.ts');
const { PINNED_WALRUS } = loader()('lib/fleet-budget.ts');
const { normalizeSuiAddress: id } = appRequire('@mysten/sui/utils');
const config = { packageId: id('0xa'), storageOrigin: id('0xb'), walrus: PINNED_WALRUS.testnet };
const policy = { executor: id('0xc'), margin: 2, wal: { value: '100' }, gas: { value: '10' },
  wal_per_epoch: '100', gas_per_epoch: '10', reward: '5', epoch: 1, wal_spent: '2', gas_spent: '0',
  revision: '1', pending_wal: '100', pending_count: '1', covered_count: '1' };
assert.deepEqual(r.RetentionBcs.parse(Retention.serialize(policy).toBytes()), policy);
assert.deepEqual(r.RenewalBcs.parse(Renewal.serialize({ target: 7, max_fee: '100', last_success_epoch: null }).toBytes()),
  { target: 7, max_fee: '100', last_success_epoch: null });
const record = { device_id: null, blob_id: '123', size: '2', encoded_size: '50', end_epoch: 5, uploaded_at_ms: '1' };
assert.deepEqual(r.RecordBcs.parse(Record.serialize(record).toBytes()), record);
const builders = [
 ['configure_device_retention', r.buildConfigureDeviceRetentionTx(config, '0x1', '0x2', '0x3', 8, 100n)],
 ['clear_device_retention', r.buildClearDeviceRetentionTx(config, '0x1', '0x2', '0x3')],
  ['migrate', r.buildMigrateRetentionTx(config, '0x1', '0x2', '0x3')],
  ['configure_retention', r.buildConfigureRetentionTx(config, '0x1', '0x2', '0x3', '0xc', 2, 100n, 10n, 5n)],
  ['authorize_renewal', r.buildAuthorizeRenewalTx(config, '0x1', '0x2', '0x3', '0x4', 7, 100n)],
  ['withdraw_retention', r.buildWithdrawRetentionTx(config, '0x1', '0x2', '0x3', 1n, 2n, '0xc')],
];
for (const [name, tx] of builders) {
  const data = Transaction.from(tx).getData();
  const call = data.commands[0].MoveCall;
  assert.equal(call.function, name);
  assert.equal(call.package, config.packageId);
  assert.equal(call.module, 'fleet_storage');
  for (const argument of call.arguments) {
    if (argument.$kind === 'Input') assert.ok(data.inputs[argument.Input]);
  }
}
assert.ok(r.retentionFacts(undefined, [], 1)[0].includes('not configured'));
assert.ok(r.retentionFacts({ ...policy, wal: { value: '99' } }, [], 1).some((x) => x.includes('underfunded')));
assert.ok(r.retentionFacts(policy, [{ id: 'x', end: 5, encodedSize: 50n }], 5).some((x) => x.includes('expired')));
const limits = { float_cap: '10', period_ms: '1000', period_allowance: '100', max_draws_per_period: '10', min_draw_interval_ms: '0' };
const budget = { id: id('0x1'), version: '1', registry_id: id('0x2'), owner_cap_id: id('0x3'), balance: { value: '100' }, paused: false,
  default_limits: limits, low_balance_threshold: '10', device_count: '1', total_deposited: '100', total_withdrawn: '0', total_paid: '0', total_allocated: '0', total_reclaimed: '0' };
assert.deepEqual(Budget.parse(generated.fleet_budget_FleetBudgetBcs.serialize(budget).toBytes()), budget);
const account = { id: id('0x4'), budget_id: id('0x1'), device_id: id('0x5'), owner_cap_id: id('0x6'), reserve: { value: '50' }, limits,
  suspended: false, closed: false, period_start_ms: '1', drawn_in_period: '0', draws_in_period: '0', last_draw_ms: '0', total_drawn: '0', enrolled_at_ms: '1' };
assert.deepEqual(Account.parse(generated.fleet_budget_GasAccountBcs.serialize(account).toBytes()), account);
// Descriptor-based key validation must reject symlinks and public permissions.
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { executorSigner } = require('./retention_transport.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'sf092-key-'));
try {
  const key = Ed25519Keypair.generate(), filename = path.join(temporary, 'executor');
  fs.writeFileSync(filename, key.getSecretKey(), { mode: 0o600 });
  assert.equal(executorSigner(filename).toSuiAddress(), key.toSuiAddress());
  fs.symlinkSync(filename, path.join(temporary, 'link'));
  assert.throws(() => executorSigner(path.join(temporary, 'link')));
  fs.chmodSync(filename, 0o644);
  assert.throws(() => executorSigner(filename));
} finally { fs.rmSync(temporary, { recursive: true, force: true }); }
const authorization = r.buildAuthorizeRenewalTx(config, '0x1', '0x2', '0x3', '0x4', 7, 100n).getData();
assert.equal(authorization.commands[1].MoveCall.function, 'prepay_renewal');
console.log('PASS: retention SDK builders, cross-runtime BCS, independent funding/expiry facts');

(async () => {
  const { renewalReceipt } = require('./retention_transport.cjs');
  const tx = new Transaction();
  tx.setSender(id('0xc')); tx.setGasBudget(10); tx.setGasPrice(1);
  const ref = (objectId) => ({ objectId: id(objectId), version: '1', digest: '11111111111111111111111111111111' });
  tx.setGasPayment([ref('0x10')]);
  tx.transferObjects([tx.objectRef(ref('0x11'))], id('0xc'));
  const bytes = await tx.build();
  const value = { bytes: Buffer.from(bytes).toString('base64'), digest: await Transaction.from(bytes).getDigest() };
  let advanced = '', calls = 0, indexed = false;
  const client = {
    async getTransaction() {
      calls++;
      if (indexed && calls > 1) return { Transaction: { status: { success: true } } };
      throw Object.assign(Error('not indexed'), { code: 5 });
    },
    async getObject({ objectId }) { return { object: { version: objectId === advanced ? '2' : '1' } }; },
  };
  assert.equal((await renewalReceipt(client, value)).status, 'unknown');
  for (advanced of [id('0x10'), id('0x11')]) {
    calls = 0;
    assert.equal((await renewalReceipt(client, value)).status, 'failed');
    assert.equal(calls, 2);
  }
  calls = 0; indexed = true;
  assert.equal((await renewalReceipt(client, value)).status, 'success');
  indexed = false;
  client.getObject = async () => { throw Error('unavailable'); };
  await assert.rejects(renewalReceipt(client, value), /unavailable/);
  await assert.rejects(renewalReceipt(client, { ...value, digest: 'wrong' }));
  console.log('PASS: signed gas/owned reference invalidation, unchanged refs, indexing race and read failure');
})().catch((error) => { console.error(error); process.exitCode = 1; });

(async()=>{
 const origins={DeviceRetentionKey:id('0x91'),DeviceRetention:id('0x92')};let captured;
 const client={movePackageService:{getDatatype:async({name})=>({response:{datatype:{definingId:origins[name]}}})},
  async getDynamicField(request){captured=request;return {dynamicField:{value:{type:`${origins.DeviceRetention}::fleet_storage::DeviceRetention`,bcs:r.DeviceRetentionBcs.serialize({target:8,max_fee:'100'}).toBytes()}}};}};
 assert.deepEqual(await r.fetchDeviceRetention(client,config,'0x1'),{target:8,max_fee:'100'});
 assert.equal(captured.name.type,`${origins.DeviceRetentionKey}::fleet_storage::DeviceRetentionKey`,'later key introduction is independent of original storage and retention origins');
 client.getDynamicField=async()=>({dynamicField:{value:{type:`${id('0x99')}::fleet_storage::DeviceRetention`,bcs:r.DeviceRetentionBcs.serialize({target:8,max_fee:'100'}).toBytes()}}});
 await assert.rejects(r.fetchDeviceRetention(client,config,'0x1'),/Foreign/);
 for(const [target,fee] of [[0,1n],[-1,1n],[0x100000000,1n],[8,0n]])assert.throws(()=>r.buildConfigureDeviceRetentionTx(config,'0x1','0x2','0x3',target,fee));
 console.log('PASS: owner finite device policy builders and independent later introduction origins');
})().catch(e=>{console.error(e);process.exitCode=1});

assert.ok(!r.retentionFacts(policy, [{ id: 'paid', end: 7, encodedSize: 50n }], 1).some(x => x.includes('closure is incomplete')),
  'fully purchased archives count as covered without a renewal policy');
(async () => {
  const { bcs } = appRequire('@mysten/sui/bcs');
  let page = 0;
  const client = { async listDynamicFields() {
    const start = page++ * 100;
    return { dynamicFields: Array.from({length: 100}, (_, offset) => ({
      name: { type: config.storageOrigin + '::fleet_storage::RecordKey',
        bcs: bcs.Address.serialize(id('0x' + (start + offset + 1).toString(16))).toBytes() },
      value: { bcs: r.RecordBcs.serialize(record).toBytes() }
    })), hasNextPage: page < 100, cursor: String(page) };
  }};
  assert.equal((await r.fetchRetention(client, '0x1', config.packageId, config.storageOrigin)).blobs.length, 10000);
  assert.equal(page, 100);
  console.log('PASS: 100-page/10000-blob retention view and fully-paid archive coverage');
})().catch(e => { console.error(e); process.exitCode = 1; });
