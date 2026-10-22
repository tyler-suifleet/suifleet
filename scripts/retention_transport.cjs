// Private owner-run executor transport. JSON stdin/stdout, never owner keys/caps.
const fs = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const app = createRequire(path.resolve(__dirname, '../dapp/package.json'));
const { SuiGrpcClient } = app('@mysten/sui/grpc');
const { Transaction } = app('@mysten/sui/transactions');
const { Ed25519Keypair } = app('@mysten/sui/keypairs/ed25519');
const { bcs } = app('@mysten/sui/bcs');
const { normalizeSuiAddress: id, normalizeStructTag: tag } = app('@mysten/sui/utils');
const balance = bcs.struct('Balance', { value: bcs.u64() });
const Retention = bcs.struct('Retention', {
  executor: bcs.Address, margin: bcs.u32(), wal: balance, gas: balance,
  wal_per_epoch: bcs.u64(), gas_per_epoch: bcs.u64(), reward: bcs.u64(),
  epoch: bcs.u32(), wal_spent: bcs.u64(), gas_spent: bcs.u64(), revision: bcs.u64(),
  pending_wal: bcs.u64(), pending_count: bcs.u64(), covered_count: bcs.u64(),
});
const Renewal = bcs.struct('Renewal', { target: bcs.u32(), max_fee: bcs.u64(), last_success_epoch: bcs.option(bcs.u32()) });
const Record = bcs.struct('BlobRecord', { device_id: bcs.option(bcs.Address), blob_id: bcs.u256(), size: bcs.u64(),
  encoded_size: bcs.u64(), end_epoch: bcs.u32(), uploaded_at_ms: bcs.u64() });
const Limits = bcs.struct('Limits', { float_cap: bcs.u64(), period_ms: bcs.u64(), period_allowance: bcs.u64(), max_draws_per_period: bcs.u64(), min_draw_interval_ms: bcs.u64() });
const Budget = bcs.struct('FleetBudget', { id: bcs.Address, version: bcs.u64(), registry_id: bcs.Address, owner_cap_id: bcs.Address,
  balance, paused: bcs.bool(), default_limits: Limits, low_balance_threshold: bcs.u64(), device_count: bcs.u64(),
  total_deposited: bcs.u64(), total_withdrawn: bcs.u64(), total_paid: bcs.u64(), total_allocated: bcs.u64(), total_reclaimed: bcs.u64() });
const Account = bcs.struct('GasAccount', { id: bcs.Address, budget_id: bcs.Address, device_id: bcs.Address, owner_cap_id: bcs.Address,
  reserve: balance, limits: Limits, suspended: bcs.bool(), closed: bcs.bool(), period_start_ms: bcs.u64(), drawn_in_period: bcs.u64(),
  draws_in_period: bcs.u64(), last_draw_ms: bcs.u64(), total_drawn: bcs.u64(), enrolled_at_ms: bcs.u64() });
function check(ok) { if (!ok) throw Error('retention configuration or authority mismatch'); }
async function run({ config: c, action, value }) {
  check(c.chain === '4c78adac' && c.network === 'testnet');
  for (const key of ['storage', 'package', 'storage_origin', 'retention_origin', 'executor']) id(c[key]);
  check(BigInt(c.gas_budget) > 0n && BigInt(c.gas_budget) <= 50_000_000n);
  const client = new SuiGrpcClient({ network: 'testnet', baseUrl: c.rpc });
  check((await client.getChainIdentifier()).chainIdentifier === c.chain);
  const storage = (await client.getObject({ objectId: c.storage, include: { content: true } })).object;
  check(tag(storage.type) === tag(`${c.storage_origin}::fleet_storage::FleetStorage`) && storage.owner.$kind === 'Shared');
  // Existing layout starts UID, version. Require explicitly migrated version 2.
  check(bcs.u64().parse(storage.content.slice(32, 40)) === '2');
  async function field(name, key, layout, origin = c.retention_origin) {
    const result = await client.getDynamicField({ parentId: c.storage,
      name: { type: `${origin}::fleet_storage::${name}`, bcs: key } });
    check(tag(result.dynamicField.value.type) === tag(`${origin}::fleet_storage::${layout.name}`));
    return layout.parse(result.dynamicField.value.bcs);
  }
  if (action === 'snapshot') {
    const { walrus, TESTNET_WALRUS_PACKAGE_CONFIG } = app('@mysten/walrus');
    const wal = client.$extend(walrus({ packageConfig: TESTNET_WALRUS_PACKAGE_CONFIG })).walrus;
    const system = await wal.systemObject();
    check(id(system.id) === id(c.system) && id(system.package_id) === id(c.walrus_package));
    const ws = await wal.systemState();
    const epoch = ws.committee.epoch;
    const r = await field('RetentionKey', Uint8Array.of(0), Retention);
    check(id(r.executor) === id(c.executor));
    const blobs = [];
    let cursor;
    do {
      const page = await client.listDynamicFields({ parentId: c.storage, cursor, limit: 100, include: { value: true } });
      for (const entry of page.dynamicFields) {
        if (tag(entry.name.type) !== tag(`${c.retention_origin}::fleet_storage::RenewalKey`)) continue;
        check(blobs.length < 1024);
        const blob = bcs.Address.parse(entry.name.bcs);
        check(tag(entry.valueType) === tag(`${c.retention_origin}::fleet_storage::Renewal`));
        const p = Renewal.parse(entry.value.bcs);
        const record = await field('RecordKey', bcs.Address.serialize(blob).toBytes(), Record, c.storage_origin);
        const ceiling = BigInt(p.max_fee);
        const quoted = ((BigInt(record.encoded_size) + 1048575n) / 1048576n) * BigInt(ws.storage_price_per_unit_size) * BigInt(Math.max(0, p.target - record.end_epoch));
        const spent = r.epoch === epoch ? BigInt(r.wal_spent) : 0n;
        const gasSpent = r.epoch === epoch ? BigInt(r.gas_spent) : 0n;
        blobs.push({ id: blob, end: record.end_epoch, target: p.target,
          quote: quoted.toString(), last_success_epoch: p.last_success_epoch,
          affordable: quoted <= ceiling && BigInt(r.wal.value) >= ceiling &&
            BigInt(r.gas.value) >= BigInt(r.reward) && ceiling + spent <= BigInt(r.wal_per_epoch) &&
            BigInt(r.reward) + gasSpent <= BigInt(r.gas_per_epoch) });
      }
      cursor = page.hasNextPage ? page.cursor : undefined;
    } while (cursor);
    const warnings = [];
    if (BigInt(r.wal.value) < BigInt(r.pending_wal) || BigInt(r.gas.value) < BigInt(r.pending_count) * BigInt(r.reward)) warnings.push('retention_low');
    // FleetStorage WAL offset follows variable Option<Storage>; read via exact layout.
    const Storage = bcs.struct('Storage', { id: bcs.Address, start: bcs.u32(), end: bcs.u32(), size: bcs.u64() });
    const Quota = bcs.struct('Quota', { period: bcs.u64(), bytes: bcs.u64(), uploads: bcs.u64(), size: bcs.u64(), fees: bcs.u64() });
    const Fleet = bcs.struct('FleetStorage', { id: bcs.Address, version: bcs.u64(), budget: bcs.Address,
      pool: bcs.option(Storage), wal: balance, quota: Quota, count: bcs.u64(), bytes: bcs.u64(), fees: bcs.u64() });
    const fleet = Fleet.parse(storage.content);
    if (BigInt(fleet.count) !== BigInt(r.covered_count)) warnings.push('closure_incomplete');
    if (BigInt(fleet.wal.value) <= BigInt(c.low_wal || '0')) warnings.push('spendable_low');
    if (c.budget_origin) {
      const object = (await client.getObject({ objectId: fleet.budget, include: { content: true } })).object;
      check(tag(object.type) === tag(`${c.budget_origin}::fleet_budget::FleetBudget`) && object.owner.$kind === 'Shared');
      const budget = Budget.parse(object.content);
      check(budget.version === '1');
      if (BigInt(budget.balance.value) < BigInt(budget.low_balance_threshold)) warnings.push('operation_balance_low');
      if (budget.paused) warnings.push('operation_budget_paused');
      const clockObject = (await client.getObject({ objectId: '0x6', include: { content: true } })).object;
      check(tag(clockObject.type) === tag('0x2::clock::Clock'));
      const clockMs = BigInt(bcs.u64().parse(clockObject.content.slice(32)));
      let accountCursor, count = 0;
      do {
        const page = await client.listDynamicFields({ parentId: fleet.budget, cursor: accountCursor, limit: 100, include: { value: true } });
        for (const entry of page.dynamicFields) {
          if (tag(entry.name.type) !== tag(`${c.budget_origin}::fleet_budget::DeviceKey`)) continue;
          check(++count <= 1024 && tag(entry.valueType) === tag('0x2::object::ID'));
          const accountId = bcs.Address.parse(entry.value.bcs);
          const accountObject = (await client.getObject({ objectId: accountId, include: { content: true } })).object;
          check(tag(accountObject.type) === tag(`${c.budget_origin}::fleet_budget::GasAccount`) && accountObject.owner.$kind === 'Shared');
          const a = Account.parse(accountObject.content);
          check(id(a.budget_id) === id(fleet.budget) && id(a.device_id) === id(bcs.Address.parse(entry.name.bcs)));
          if (a.closed || a.suspended) continue;
          if (BigInt(a.reserve.value) === 0n) warnings.push('operation_reserve_empty');
          if (clockMs < BigInt(a.period_start_ms) + BigInt(a.limits.period_ms) &&
              (BigInt(a.drawn_in_period) >= BigInt(a.limits.period_allowance) || BigInt(a.draws_in_period) >= BigInt(a.limits.max_draws_per_period))) warnings.push('operation_allowance_exhausted');
        }
        accountCursor = page.hasNextPage ? page.cursor : undefined;
      } while (accountCursor);
    }
    const staking = await wal.stakingState();
    return { epoch, margin: r.margin, revision: r.revision, blobs, warnings: [...new Set(warnings)],
      reserve_wal: r.wal.value, reserve_gas: r.gas.value, spendable_wal: fleet.wal.value,
      epoch_duration_ms: staking.epoch_duration, first_epoch_start_ms: staking.first_epoch_start };
  }
  if (action === 'receipt') {
    try {
      const result = await client.getTransaction({ digest: value.digest, include: { effects: true } });
      return { status: result.Transaction?.status.success ? 'success' : 'failed' };
    } catch (error) {
      if (error.reason === 'notFound' || error.code === 'NOT_FOUND' || error.code === 5) return { status: 'unknown' };
      throw error;
    }
  }
  // Only an executor-specific key file; never a Sui wallet/owner keystore.
  const stat = fs.statSync(c.executor_key_file);
  check((stat.mode & 0o077) === 0 && stat.size < 1024);
  const privateKey = fs.readFileSync(c.executor_key_file, 'utf8').trim();
  const signer = Ed25519Keypair.fromSecretKey(/^[0-9a-f]{64}$/i.test(privateKey) ? Uint8Array.from(Buffer.from(privateKey, 'hex')) : privateKey);
  check(id(signer.toSuiAddress()) === id(c.executor));
  if (action === 'prepare') {
    const tx = new Transaction();
    tx.setSender(c.executor); tx.setGasBudget(BigInt(c.gas_budget));
    tx.moveCall({ target: `${c.package}::fleet_storage::execute_renewal`, arguments: [tx.object(c.storage), tx.object(c.system),
      tx.pure.id(value.blob), tx.pure.u32(value.end), tx.pure.u64(value.revision)] });
    const bytes = await tx.build({ client });
    const signature = await signer.signTransaction(bytes);
    return { digest: await Transaction.from(bytes).getDigest(), bytes: Buffer.from(bytes).toString('base64'), signature: signature.signature };
  }
  if (action === 'submit') {
    const bytes = Buffer.from(value.bytes, 'base64');
    check(bytes.length <= 16384 && await Transaction.from(bytes).getDigest() === value.digest);
    const tx = Transaction.from(bytes).getData();
    check(id(tx.sender) === id(c.executor) && BigInt(tx.gasData.budget) === BigInt(c.gas_budget) && tx.commands.length === 1);
    const call = tx.commands[0].MoveCall;
    check(call && id(call.package) === id(c.package) && call.module === 'fleet_storage' && call.function === 'execute_renewal');
    // Re-sign only exactly the transaction this executor generated; immutable bytes
    // keep gas and object references identical across retries/restarts.
    check((await signer.signTransaction(bytes)).signature === value.signature);
    const result = await client.executeTransaction({ transaction: bytes, signatures: [value.signature], include: { effects: true } });
    check(result.Transaction?.status.success);
    await client.waitForTransaction({ digest: value.digest });
    return { status: 'submitted' }; // worker separately verifies expiry + receipt
  }
  throw Error('unknown retention action');
}
if (require.main === module) {
  let input = '';
  process.stdin.on('data', (chunk) => { input += chunk; if (input.length > 2 * 1024 * 1024) process.exit(1); });
  process.stdin.on('end', async () => {
    try { process.stdout.write(JSON.stringify(await run(JSON.parse(input)))); }
    catch { process.stderr.write('retention transport failed\n'); process.exitCode = 1; }
  });
}
module.exports = { run, Retention, Renewal, Record, Budget, Account };
