// Independent read-only gRPC acceptance. No wallet, signing or mutation.
const fs = require('node:fs/promises');
const assert = require('node:assert/strict');
const { appRequire, loader } = require('./frontend_test_utils.cjs');
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
(async () => {
  const [manifestPath, evidencePath] = process.argv.slice(2);
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  const evidence = JSON.parse(await fs.readFile(evidencePath, 'utf8'));
  const protocol = loader()('lib/fleet-protocol.ts');
  const client = new SuiGrpcClient({ network: 'testnet', baseUrl: manifest.rpc_url });
  const { fromBase58, toHex } = appRequire('@mysten/sui/utils');
  assert.equal(toHex(fromBase58((await client.getChainIdentifier()).chainIdentifier).slice(0, 4)), '4c78adac');
  const amounts = { Deposited: [], Collected: [], Refunded: [] }, effects = [];
  const origin = manifest.type_origins['subscription::Account'];
  for (const entry of evidence.transactions) {
    assert.ok(['success', 'failure'].includes(entry.status));
    const result = await client.getTransaction({ digest: entry.digest, include: { effects: true, events: true } });
    assert.equal(result.$kind, entry.status === 'success' ? 'Transaction' : 'FailedTransaction');
    const tx = result.Transaction ?? result.FailedTransaction;
    assert.equal(tx.digest, entry.digest);
    assert.equal(tx.status.success, entry.status === 'success');
    const gas = tx.effects.gasUsed;
    const net = BigInt(gas.computationCost) + BigInt(gas.storageCost) - BigInt(gas.storageRebate);
    assert.equal(BigInt(entry.gas_mist), net > 0n ? net : 0n);
    effects.push({ label: entry.label, digest: tx.digest, status: tx.status, gas, signed_net_gas_mist: net.toString() });
    for (const event of tx.events ?? []) {
      for (const name of Object.keys(amounts)) {
        if (event.eventType !== `${origin}::subscription::${name}`) continue;
        const parsed = protocol[`subscription_${name}Bcs`].parse(event.bcs);
        assert.equal(parsed.account_id, evidence.objects.account);
        amounts[name].push(BigInt(parsed.amount).toString());
        if (name === 'Refunded') assert.equal(parsed.recipient, evidence.operator);
      }
    }
  }
  assert.deepEqual(amounts, { Deposited: ['108000', '1000'], Collected: ['9000'], Refunded: ['100000'] });
  const read = await client.core.getObjects({ objectIds: [evidence.objects.account], include: { content: true } });
  const account = protocol.subscription_AccountBcs.parse(read.objects[0].content);
  assert.equal(account.cancelled, true);
  assert.equal(BigInt(account.balance.value), 0n);
  assert.equal(BigInt(account.remaining), 0n);
  assert.equal(account.treasury, manifest.treasury_payee);
  assert.equal(account.customer_id, evidence.objects.customer);
  assert.equal(BigInt(account.fee), 9000n);
  evidence.independent_grpc = { chain: '4c78adac', effects, amounts, account_cancelled: true, account_balance: '0' };
  await fs.writeFile(evidencePath, JSON.stringify(evidence, (_, v) => typeof v === 'bigint' ? v.toString() : v, 2) + '\n');
  console.log('PASS: independent gRPC effects/gas, canonical typed deposit/collection/refund events and zero-balance cancelled account');
})().catch(error => { console.error(error.message); process.exitCode = 1; });
