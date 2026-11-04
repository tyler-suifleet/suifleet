const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader, appRequire, manifestClient } = require('./frontend_test_utils.cjs');
const m = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
const addr = n => '0x' + n.toString(16).padStart(64, '0');
m.treasury_payee = addr(99);
m.subscription_coins = { USDC: { coin_type: addr(87) + '::usdc::USDC', decimals: 6 }, SUI: { coin_type: addr(2) + '::sui::SUI', decimals: 9 } };
for (const name of [...loader()('lib/protocol-version.ts').CUSTOMER_TYPES, ...loader()('lib/protocol-version.ts').SUBSCRIPTION_TYPES]) m.type_origins[name] = m.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const load = loader(), sub = load('lib/subscription.ts'), protocol = load('lib/fleet-protocol.ts');
const now = Date.now(), q = { offerId: addr(11), symbol: 'USDC', usdPerCoin: 1, observedAt: now - 1000, source: 'USDC USD quote fixture' };
const offer = { id: q.offerId, registry_id: m.shared_objects.registry.id, treasury: m.treasury_payee, tier: '1', device_limit: '100', allowance: '100', fee: '49000000', periods: '1', period_ms: '2592000000', published_at_ms: String(now), expires_at_ms: String(now + 86400000) };
const account = { id: addr(12), customer_id: addr(13), registry_id: offer.registry_id, treasury: m.treasury_payee, balance: { value: '49000000' }, tier: '1', device_limit: '100', allowance: '100', fee: offer.fee, remaining: '1', period_ms: offer.period_ms, next_due_ms: String(now), term_end_ms: String(now + 2592000000), cancelled: false };
const payment = { chain: m.chain_identifier, package: m.call_package_id, account: account.id, customer: account.customer_id, symbol: 'USDC', amount: '49000000' };
assert.equal(sub.validateOffer(offer, q, now).total, 49000000n);
assert.equal(sub.validateOffer({ ...offer, periods: '12', period_ms: '2628000000', fee: '44100000' }, q, now).total, 529200000n);
for (const patch of [{ treasury: addr(98) }, { registry_id: addr(98) }, { id: addr(98) }, { fee: '1' }, { fee: '49000001' }, { periods: '2' }, { period_ms: '1' }, { expires_at_ms: String(now - 1) }]) {
  assert.throws(() => sub.validateOffer({ ...offer, ...patch }, q, now));
}
assert.throws(() => sub.validateOffer({ ...offer, fee: '49000002' }, q, now), /price/);
assert.throws(() => sub.validateOffer(offer, { ...q, observedAt: now + 1 }, now), /conversion/);
assert.throws(() => sub.validateOffer(offer, { ...q, observedAt: now - 86400001 }, now), /conversion/);
assert.throws(() => sub.quotes(JSON.stringify([q, q])), /Duplicate/);
const uri = sub.paymentUri('https://console.example', payment);
assert.deepEqual(sub.parsePayment(new URL(uri).hash), payment);
assert.throws(() => sub.parsePayment(new URL(uri).hash + '&amount=1'), /Malformed/);
for (const patch of [{ chain: 'deadbeef' }, { package: addr(98) }, { amount: '0' }, { amount: '-1' }, { amount: '1e6' }, { amount: '18446744073709551616' }, { symbol: 'WAL' }, { symbol: '__proto__' }]) assert.throws(() => sub.validatePayment({ ...payment, ...patch }));
assert.equal(sub.depositTx(account, 'USDC', '100').getData().commands.at(-1).MoveCall.function, 'deposit');
assert.equal(sub.collectTx(account, 'USDC').getData().commands[0].MoveCall.function, 'collect');
assert.ok(sub.warnings({ ...account, balance: { value: '1' }, term_end_ms: String(now + 10) }, now).some(w => w.includes('Low')));
assert.ok(sub.warnings({ ...account, term_end_ms: String(now - 1) }, now).some(w => w.includes('expired')));
assert.ok(sub.warnings({ ...account, cancelled: true }, now).some(w => w.includes('cancelled')));
const csv = sub.receiptsCsv([{ kind: 'Deposited', digest: '=formula', amount: '1', accountId: account.id, checkpoint: '10', eventIndex: 0, timestampMs: 1700000000000, throughMs: null }], '=company', 'a,"b\ncity', '@tax', 'USDC');
assert.ok(csv.includes('"\'=company"')); assert.ok(csv.includes('a,""b\ncity')); assert.ok(csv.includes('"\'=formula"'));
let result, signs = 0, funds = '50000000', network = m.network, connected = { address: addr(22), chains: [`sui:${m.network}`] }, rejection = false;
const client = manifestClient(m), originalGetObject = client.getObject.bind(client);
client.getObject = async args => args.objectId === account.id ? { object: { type: `${m.call_package_id}::subscription::Account<${m.subscription_coins.USDC.coin_type}>`, owner: { $kind: 'Shared' }, content: protocol.subscription_AccountBcs.serialize(account).toBytes() } } : args.objectId === q.offerId ? { object: { type: `${m.call_package_id}::subscription::Offer<${m.subscription_coins.USDC.coin_type}>`, owner: { $kind: 'Immutable' }, content: protocol.subscription_OfferBcs.serialize(offer).toBytes() } } : originalGetObject(args);
client.getBalance = async () => ({ balance: { balance: funds } });
let existing = false;
const savedGet = client.getObject;
client.getObject = async args => args.objectId === account.customer_id ? { object: { type: `${m.type_origins['customer::Customer']}::customer::Customer`, content: protocol.customer_CustomerBcs.serialize({ id: account.customer_id, version: '201', registry_id: offer.registry_id, name: 'Fixture', credentials: { contents: [{ key: addr(22), value: true }] }, administrators: '1', fleets: [], default_fleet: null, active_devices: '0', device_limit: '100', allowance: '100', used: '0', period: '0', charge_type: [] }).toBytes() } } : savedGet(args);
client.listDynamicFields = async () => ({ dynamicFields: existing ? [{ name: { type: `${m.call_package_id}::customer::SubscriptionKey` }, value: { bcs: appRequire('@mysten/sui/bcs').bcs.Address.serialize(account.id).toBytes() } }] : [], hasNextPage: false });
const good = () => ({ Transaction: { digest: 'fixture-digest', status: { success: true }, events: [{ eventType: `${m.call_package_id}::subscription::Deposited`, bcs: protocol.subscription_DepositedBcs.serialize({ account_id: account.id, amount: payment.amount }).toBytes() }] } });
client.waitForTransaction = async () => result;
client.defaultNameServiceName = async () => ({ data: { name: 'suifleet.sui' } });
const wallet = { stores: { $connection: { get: () => ({ account: connected }) }, $currentNetwork: { get: () => network } }, signAndExecuteTransaction: async () => { signs++; if (rejection) throw Object.assign(new Error('User rejected the request'), { code: 4001 }); return result; } };
let policyReads = 0, policyUnavailable = false;
const hook = loader({
  '@/lib/security-policy': { fetchOrganizationSecurity: async () => {
    policyReads++;
    if (policyUnavailable) throw Error('Unsupported organization authority version');
    return { requirements: [2] };
  } },
  '@/lib/passkey-factor': {
    approvalDecisionsOnly: () => false,
    enrolledSigner: async () => { throw Error('Deposit must not request a passkey'); },
    withSponsorFunding: () => { throw Error('Deposit must remain wallet-paid'); },
  },
  react: { useRef: current => ({ current }) },
  './useFleetSelection': { useFleetSelection: () => ({ supported: true, fleet: { id: addr(90), customerId: addr(91), administrator: false } }) },
  '@tanstack/react-query': { useMutation: o => o, useQueryClient: () => ({ invalidateQueries() {} }) },
  '@mysten/dapp-kit-react': { useCurrentClient: () => client, useDAppKit: () => wallet },
})('hooks/useFleetTransaction.ts');
(async () => {
  const events = {
    Deposited: [{ checkpoint: '10', eventIndex: 2, transactionDigest: 'deposit', bcs: protocol.subscription_DepositedBcs.serialize({ account_id: account.id, amount: '3' }).toBytes() }],
    Collected: [{ checkpoint: '9', eventIndex: 0, transactionDigest: 'collect', bcs: protocol.subscription_CollectedBcs.serialize({ account_id: account.id, amount: '2', through_ms: '1700000060000' }).toBytes() }],
    Refunded: [{ checkpoint: '10', eventIndex: 1, transactionDigest: 'refund', bcs: protocol.subscription_RefundedBcs.serialize({ account_id: account.id, recipient: addr(22), amount: '1' }).toBytes() }],
  };
  const historySub = loader({ './event-history': { incrementalEvents: async (_client, type) => events[type.split('::').at(-1)] } })('lib/subscription.ts');
  client.getTransaction = async () => ({ $kind: 'Transaction', Transaction: { timestampMs: 1700000000000 } });
  const rows = await historySub.history(client, account.id);
  assert.deepEqual(rows.map(r => r.kind), ['Collected', 'Refunded', 'Deposited']);
  assert.equal(rows[0].throughMs, '1700000060000');
  assert.equal(rows[1].throughMs, null);
  const datedCsv = historySub.receiptsCsv(rows, 'Company', 'Address', 'Tax', 'USDC');
  assert.match(datedCsv, /Date \(UTC\).*Checkpoint.*Event index.*Collected through \(UTC\)/);
  assert.match(datedCsv, /2023-11-14T22:13:20.000Z/);
  assert.match(datedCsv, /2023-11-14T22:14:20.000Z/);
  assert.ok(datedCsv.indexOf('Collected",') < datedCsv.indexOf('Refunded",'));
  client.getTransaction = async () => ({ $kind: 'Transaction', Transaction: { timestampMs: null } });
  assert.equal((await historySub.history(client, account.id))[0].timestampMs, null);
  assert.equal(historySub.receiptDate(null), 'Unavailable');
  client.getTransaction = async () => { throw Error('pruned transaction'); };
  await assert.rejects(historySub.history(client, account.id), /pruned transaction/);
  result = good();
  const opened = await sub.preparePrepay(client, q, account.customer_id, addr(22));
  assert.equal(opened.getData().commands.at(-1).MoveCall.function, 'open');
  await assert.rejects(sub.preparePrepay(client, q, account.customer_id, addr(23)), /administrator/);
  existing = true;
  await assert.rejects(sub.preparePrepay(client, q, account.customer_id, addr(22)), /term must end/);
  account.cancelled = true;
  await assert.rejects(sub.preparePrepay(client, q, account.customer_id, addr(22)), /30 days/);
  const oldPublished = offer.published_at_ms;
  offer.published_at_ms = String(now - 31 * 86400000);
  const renewal = await sub.preparePrepay(client, { ...q, observedAt: Number(offer.published_at_ms) }, account.customer_id, addr(22));
  assert.equal(renewal.getData().commands.at(-1).MoveCall.function, 'renew');
  offer.published_at_ms = oldPublished;
  result = good(); result.Transaction.events.push({ eventType: `${m.call_package_id}::subscription::PlanChanged`, bcs: protocol.subscription_PlanChangedBcs.serialize({ account_id: account.id, offer_id: offer.id, tier: offer.tier, fee: offer.fee, periods: offer.periods }).toBytes() });
  await sub.verifyPlan(client, 'fixture-digest', account.customer_id, q);
  result.Transaction.events = [];
  await assert.rejects(sub.verifyPlan(client, 'fixture-digest', account.customer_id, q), /does not confirm/);
  result = good();
  account.cancelled = false; existing = false;
  await sub.readOffer(client, q); await sub.readAccount(client, account.id, 'USDC', account.customer_id);
  assert.equal(await sub.treasuryLabel(client), 'suifleet.sui');
  client.defaultNameServiceName = async () => ({ data: { name: 'lookalike.sui' } }); assert.equal(await sub.treasuryLabel(client), undefined);
  assert.equal(await sub.verifyDeposit(client, 'fixture-digest', payment), 'fixture-digest');
  await assert.rejects(sub.verifyDeposit(client, 'fixture-digest', { ...payment, amount: '1' }), /does not contain/);
  result = { FailedTransaction: { status: { success: false } } }; await assert.rejects(sub.verifyDeposit(client, 'fixture-digest', payment), /failed/);
  result = good(); result.Transaction.events = []; await assert.rejects(sub.verifyDeposit(client, 'fixture-digest', payment), /does not contain/);
  result = good();
  const mutate = hook.useFleetTransaction().mutationFn, tx = new (appRequire('@mysten/sui/transactions').Transaction)();
  const request = { transaction: tx, subscriptionDeposit: payment };
  await mutate(request); assert.equal(signs, 1); // Unrelated passkey-required, read-only fleet cannot block a deposit.
  assert.equal(policyReads, 0);
  policyUnavailable = true;
  await mutate(request); assert.equal(signs, 2); // An unreadable unrelated policy cannot block payment either.
  assert.equal(policyReads, 0);
  policyUnavailable = false;
  const saved = connected; connected = null; await assert.rejects(mutate(request), /Connect/); connected = saved;
  network = 'mainnet'; await assert.rejects(mutate(request), /network/); network = m.network;
  funds = '1'; await assert.rejects(mutate(request), /Insufficient/); assert.equal(signs, 2); funds = '50000000';
  rejection = true; await assert.rejects(mutate(request), /rejected/); rejection = false;
  result = { FailedTransaction: { status: { success: false, error: { message: 'abort' } } } }; await assert.rejects(mutate(request), /abort/);
  // Render the actual billing component against query responses. Business
  // parsing/authority checks above and the browser wallet fixture stay real.
  let unavailable = false;
  const pageModule = loader({
    '@/hooks/useCurrentAccount': { useCurrentAccount: () => saved },
    '@/hooks/useFleetSelection': { useFleetSelection: () => ({ identity: 'fixture', fleet: { customerId: account.customer_id, administrator: true } }) },
    '@/hooks/useFleetTransaction': { useFleetTransaction: () => ({ isPending: false }) },
    '@mysten/dapp-kit-react': { useCurrentClient: () => client },
    '@tanstack/react-query': {
      useQuery: o => ({ isPending: false, data: o.queryKey[0] === 'subscription-quotes' ? (unavailable ? undefined : { quotes: [q], treasury: m.treasury_payee }) : o.queryKey[0] === 'subscription-account' ? { account: { ...account, term_end_ms: String(now - 60_000) }, symbol: 'USDC' } : o.queryKey[0] === 'subscription-history' ? [{ kind: 'Deposited', digest: 'fixture-history-digest', amount: '1000', accountId: account.id, checkpoint: '10', eventIndex: 0, timestampMs: 1700000000000, throughMs: '1700000060000' }] : undefined, error: unavailable && o.queryKey[0] === 'subscription-quotes' ? new Error('unconfigured') : null }),
      useQueries: () => [{ data: { offer, total: 49000000n, annual: false, descriptor: m.subscription_coins.USDC, usd: 49 } }],
    },
  })('app/billing/page.tsx');
  const React = appRequire('react'), { renderToStaticMarkup } = appRequire('react-dom/server');
  const page = renderToStaticMarkup(React.createElement(pageModule.default));
  assert.match(page, /Plans and billing/); assert.match(page, /Subscription term expired/);
  assert.match(page, /Download CSV receipts/); assert.match(page, /fixture-history-digest/); assert.match(page, /2023-11-14T22:13:20.000Z/); assert.match(page, /Collected through 2023-11-14T22:14:20.000Z/); assert.match(page, /checkpoint 10/);
  assert.match(page, /Company address/); assert.match(page, /0.001.*USDC/);
  assert.doesNotMatch(page, /docs\/BUSINESS_PLAN/);
  unavailable = true;
  assert.match(renderToStaticMarkup(React.createElement(pageModule.default)), /payments are not configured/);
  console.log('PASS: subscription offer pricing/bindings/expiry, annual rounding, payment-link guards, warnings, CSV injection, optional reverse label, effects/event confirmation and actual wallet-hook connect/sign/reject/network/insufficient-funds paths');
})().catch(e => { console.error(e); process.exitCode = 1; });
