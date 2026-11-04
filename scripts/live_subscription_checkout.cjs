// Keyless bridge: real frontend builders/readers, explicit verified manifest,
// unsigned bytes only. The bounded Python driver owns signing and spend limits.
const fs = require('node:fs'), readline = require('node:readline');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const [manifestPath, customer, offerId, sender] = process.argv.slice(2);
const m = JSON.parse(fs.readFileSync(manifestPath));
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
// Explicit disposable Testnet tariff, never the product's published price.
const table = JSON.parse(fs.readFileSync('dapp/src/lib/fleet-price-table.json'));
table.source = 'SF-182 disposable test-only tariff (not a customer price)';
table.base.monthlyMinimumUsd = 0.01;
table.base.tiers = table.base.tiers.map(t => ({ ...t, usdPerDeviceMonth: 0.00001 }));
process.env.NEXT_PUBLIC_FLEET_PRICE_TABLE = JSON.stringify(table);
const { SuiGrpcClient } = appRequire('@mysten/sui/grpc'), { toBase64 } = appRequire('@mysten/sui/utils');
const sub = loader()('lib/subscription.ts'), protocol = loader()('lib/fleet-protocol.ts');
const client = new SuiGrpcClient({ network: 'testnet', baseUrl: m.rpc_url });
const lines = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();
(async () => {
  if (customer === '--discover') {
    const { bcs } = appRequire('@mysten/sui/bcs');
    const { normalizeStructTag, normalizeSuiAddress } = appRequire('@mysten/sui/utils');
    const { moveType, verifyDeployment } = loader()('lib/deployment-manifest.ts');
    await verifyDeployment(client);
    let cursor, customerId = null;
    do {
      const page = await client.listDynamicFields({ parentId: m.shared_objects.registry.id, cursor, limit: 100, include: { value: true } });
      for (const f of page.dynamicFields) {
        if (normalizeStructTag(f.name.type) !== normalizeStructTag(moveType('customer::CustomerKey'))) continue;
        const key = protocol.customer_CustomerKeyBcs.parse(f.name.bcs);
        if (normalizeSuiAddress(key.wallet) === normalizeSuiAddress(sender)) customerId = bcs.Address.parse(f.value.bcs);
      }
      if (page.hasNextPage && (!page.cursor || page.cursor === cursor)) throw Error('Incomplete customer discovery');
      cursor = page.hasNextPage ? page.cursor : undefined;
    } while (cursor);
    const existing = customerId ? await sub.customerAccount(client, customerId) : null;
    let offer = null, eligibleAt = null, reason = null;
    if (offerId !== '-') {
      const quoteObject = await client.getObject({ objectId: offerId, include: { content: true } });
      if (normalizeStructTag(quoteObject.object.type) !== normalizeStructTag(`${moveType('subscription::Offer')}<${m.subscription_coins.USDC.coin_type}>`)) throw Error('Unexpected offer type');
      offer = protocol.subscription_OfferBcs.parse(quoteObject.object.content);
      const quote = { offerId, symbol: 'USDC', usdPerCoin: 1, observedAt: Number(offer.published_at_ms), source: 'Disposable test denomination' };
      await sub.readOffer(client, quote);
      eligibleAt = existing ? Number(offer.published_at_ms) + 30 * 86400000 : Number(offer.published_at_ms);
      if (existing && (!existing.account.cancelled || existing.account.balance.value !== '0')) reason = 'Disposable renewal fixture must be cancelled and empty';
      else if (existing && Date.now() < eligibleAt) reason = 'Renewal requires 30-day notice';
      else if (customerId) await sub.preparePrepay(client, quote, customerId, sender);
    } else if (existing) reason = 'Bound customer requires an explicitly selected compatible offer';
    console.log(JSON.stringify({ customer: customerId, existing, offer, action: existing ? 'renew' : 'open', eligible_at_ms: eligibleAt, eligible_at_utc: eligibleAt === null ? null : new Date(eligibleAt).toISOString(), reason }));
    return;
  }
  if (customer === '--verify-evidence') {
    const evidence = JSON.parse(fs.readFileSync(offerId));
    const expected = evidence.checkout.account.id, effects = [], events = [];
    for (const entry of evidence.transactions) {
      const result = await client.getTransaction({ digest: entry.digest, include: { effects: true, events: true } });
      if (result.FailedTransaction || !result.Transaction.status.success) throw Error('Unsuccessful recorded transaction');
      const gas = result.Transaction.effects.gasUsed;
      const net = BigInt(gas.computationCost) + BigInt(gas.storageCost) - BigInt(gas.storageRebate);
      if ((net > 0n ? net : 0n) !== BigInt(entry.gas_mist)) throw Error('Gas ledger differs from effects');
      effects.push({ digest: entry.digest, gas, signed_net_gas_mist: net.toString() });
      for (const e of result.Transaction.events ?? []) {
        for (const name of ['Deposited', 'Refunded']) if (e.eventType === `${m.type_origins['subscription::' + name]}::subscription::${name}`) {
          const value = protocol['subscription_' + name + 'Bcs'].parse(e.bcs);
          if (value.account_id !== expected || value.amount !== '1000') throw Error('Unexpected deposit/refund receipt');
          events.push({ kind: name, digest: entry.digest, ...value });
        }
      }
    }
    if (events.length !== 2 || events[0].kind !== 'Deposited' || events[1].kind !== 'Refunded') throw Error('Missing deposit or refund event');
    const final = await sub.readAccount(client, expected, 'USDC');
    if (!final.cancelled || final.balance.value !== '0' || final.remaining !== '0') throw Error('Fixture was not restored to cancelled/empty');
    evidence.independent_grpc = { effects, events, final_account: final };
    fs.writeFileSync(offerId, JSON.stringify(evidence, (_, v) => typeof v === 'bigint' ? v.toString() : v, 2) + '\n');
    console.log(JSON.stringify({ verified: true, account_cancelled: true, balance: '0', signed_gas_verified: true }));
    return;
  }
  if (customer === '--existing-account') {
    const account = await sub.readAccount(client, offerId, 'USDC');
    if (!account.cancelled || account.balance.value !== '0') throw Error('Explicit fixture must be cancelled with zero balance');
    const payment = { chain: m.chain_identifier, package: m.call_package_id, account: account.id, customer: account.customer_id, symbol: 'USDC', amount: '1000' };
    const uri = sub.paymentUri('https://acceptance.invalid', payment), parsed = sub.parsePayment(new URL(uri).hash);
    const tx = sub.depositTx(account, parsed.symbol, parsed.amount); tx.setSender(sender); tx.setGasBudget(20000000);
    console.log(JSON.stringify({ label: 'deposit', tx: toBase64(await tx.build({ client })) }));
    const { digest } = JSON.parse((await lines.next()).value);
    await sub.verifyDeposit(client, digest, parsed);
    const rows = await sub.history(client, account.id);
    if (!rows.some(r => r.digest === digest && r.kind === 'Deposited')) throw Error('Missing receipt');
    const csv = sub.receiptsCsv(rows, 'SF-182 Test Company', 'Disposable Testnet fixture', 'TEST', 'USDC');
    const after = await sub.readAccount(client, account.id, 'USDC');
    if (after.balance.value !== '1000' || !after.cancelled) throw Error('Deposit changed subscription state unexpectedly');
    console.log(JSON.stringify({ result: { account: after, uri, digest, receipts: rows, csv_has_company: csv.includes('SF-182 Test Company'), limits: [
      'Existing verified SF-181 cancelled disposable account explicitly selected. Deposit does not reactivate it.',
      'One dedicated signer exercises the external-wallet deposit protocol; real second-wallet acceptance remains #32/#159.',
      'Initial plan selection/open is unit-tested; SF-181 records historical on-chain open. No fresh frontend open/renewal is claimed: the agent already has a bound account and replacement offers require 30-day notice.',
      'The no-op customer-create/offer setup attempt remains charged to this same non-replenishing 0.1 SUI allowance.',
      'No package publication, faucet, original app objects, mainnet, physical or production-provider acceptance.'
    ] } }));
    return;
  }
  const object = await client.getObject({ objectId: offerId, include: { content: true } });
  const offer = protocol.subscription_OfferBcs.parse(object.object.content);
  const quote = { offerId, symbol: 'USDC', usdPerCoin: 1, observedAt: Number(offer.published_at_ms), source: 'Test-only USDC denomination: 1 USD/USDC, no market-price claim' };
  const execute = async (tx, label) => {
    tx.setSender(sender); tx.setGasBudget(20000000);
    console.log(JSON.stringify({ label, tx: toBase64(await tx.build({ client })) }));
    const answer = await lines.next(); if (answer.done) throw Error('Signer disconnected');
    const { digest } = JSON.parse(answer.value);
    const r = await client.waitForTransaction({ digest, include: { effects: true, events: true } });
    if (r.FailedTransaction || !r.Transaction.status.success) throw Error('Unsuccessful execution');
    return digest;
  };
  const existing = await sub.customerAccount(client, customer);
  const opened = await execute(await sub.preparePrepay(client, quote, customer, sender), existing ? 'renew' : 'open');
  const current = await sub.verifyPlan(client, opened, customer, quote);
  const payment = { chain: m.chain_identifier, package: m.call_package_id, account: current.account.id, customer, symbol: 'USDC', amount: '1000' };
  const uri = sub.paymentUri('https://acceptance.invalid', payment);
  const parsed = sub.parsePayment(new URL(uri).hash);
  const deposited = await execute(sub.depositTx(await sub.readAccount(client, parsed.account, parsed.symbol, parsed.customer), parsed.symbol, parsed.amount), 'deposit');
  await sub.verifyDeposit(client, deposited, parsed);
  const collected = await execute(sub.collectTx(current.account, 'USDC'), 'collect');
  const after = await sub.readAccount(client, current.account.id, 'USDC', customer);
  if (after.remaining !== '0' || after.balance.value !== '1000') throw Error('Unexpected balance or collection count');
  const rows = await sub.history(client, current.account.id);
  if (!rows.some(r => r.digest === opened) || !rows.some(r => r.digest === deposited) || !rows.some(r => r.digest === collected)) throw Error('Missing subscription receipts');
  const csv = sub.receiptsCsv(rows, 'SF-182 Test Company', 'Disposable Testnet fixture', 'TEST', 'USDC');
  console.log(JSON.stringify({ result: { account: after, quote, uri, digests: { opened, deposited, collected }, receipts: rows, csv_has_company: csv.includes('SF-182 Test Company'), limits: ['Disposable 0.01 USD test tariff, not the public canonical customer tariff.', 'One dedicated signer emulates both checkout and external-wallet deposit; manual multi-wallet acceptance remains #32/#159.'] } }));
})().then(() => process.exit(0)).catch(e => { console.error(e.message); process.exit(1); });
