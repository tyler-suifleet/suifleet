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
console.log('PASS: retention SDK builders, cross-runtime BCS, independent funding/expiry facts');
