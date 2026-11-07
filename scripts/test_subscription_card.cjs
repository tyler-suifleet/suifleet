const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader, manifestClient } = require('./frontend_test_utils.cjs');
const m = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json'));
const addr = n => '0x' + n.toString(16).padStart(64, '0');
m.treasury_payee = addr(99);
m.subscription_coins = { USDC: { coin_type: addr(87) + '::usdc::USDC', decimals: 6 } };
const proto = loader()('lib/protocol-version.ts');
for (const name of [...proto.CUSTOMER_TYPES, ...proto.SUBSCRIPTION_TYPES]) m.type_origins[name] = m.call_package_id;
process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(m);
const card = loader()('lib/subscription-card.ts');
const onramp = loader()('lib/onramp.ts');
assert.throws(() => onramp.checkoutUrl({ id: 'onramp-money', appId: '123', name: 'fixture' }, addr(1), 'USDC', '49'), /disabled/);
const url = new URL(onramp.checkoutUrl({ id: 'onramp-money', appId: '123', name: 'fixture' }, addr(1), 'SUI', '1'));
assert.equal(url.origin, 'https://onramp.money'); assert.equal(url.searchParams.get('walletAddress'), addr(1)); assert.equal(url.searchParams.get('coinCode'), 'sui');
const now = Date.now(), binding = { chain: m.chain_identifier, package: m.call_package_id, wallet: addr(1), customer: addr(2), coinType: m.subscription_coins.USDC.coin_type, quote: { offerId: addr(3), symbol: 'USDC', usdPerCoin: 1, observedAt: now - 1000, source: 'test' }, total: '49000000', expiresAt: now + 86400000 };
const p = card.startCardPurchase(binding, 2000000n, now);
const status = (b, sui = 1n, t = now, purchase = p) => card.cardStatus(purchase, binding, b, sui, t);
assert.equal(status(2000000n).state, 'waiting');
assert.equal(status(3000000n).state, 'partial');
assert.equal(status(3000000n).missing, 46000000n);
assert.equal(status(49000000n).state, 'ready');
assert.equal(status(49000000n, 0n).state, 'gas-needed');
assert.equal(status(1000000n).received, 0n); // concurrent spending never looks like an arrival
assert.equal(status(2000000n, 1n, now + 7200000).state, 'late');
assert.equal(status(49000000n, 1n, now + 7200001).state, 'ready'); // late funds still usable
assert.equal(status(49000000n, 1n, binding.expiresAt).state, 'expired');
assert.equal(status(49000000n, 1n, now, { ...p, abandoned: true }).state, 'abandoned');
assert.equal(status(49000000n, 1n, now, { ...p, submission: { state: 'approving' } }).state, 'approving');
assert.equal(status(49000000n, 1n, now, { ...p, submission: { state: 'submitted', digest: 'unknown' } }).state, 'submitted');
for (const changed of [{ wallet: addr(4) }, { customer: addr(4) }, { chain: '4c78adac' }, { package: addr(4) }, { total: '48000000' }, { coinType: 'fake' }, { quote: { ...binding.quote, source: 'changed' } }]) {
  assert.throws(() => card.cardStatus(p, { ...binding, ...changed }, 50000000n, 1n), /changed/);
}
const map = new Map(), storage = { getItem: k => map.get(k) ?? null, setItem: (k, v) => map.set(k, v) };
card.saveCardPurchase(storage, p);
assert.deepEqual(card.loadCardPurchase(storage, binding), p);
assert.equal(card.loadCardPurchase(storage, { ...binding, customer: addr(5) }), null);
const submitted = { ...p, submission: { state: 'submitted', digest: 'uncertain-digest' } };
card.saveCardPurchase(storage, submitted);
assert.equal(card.loadCardPurchase(storage, binding).submission.digest, 'uncertain-digest');
map.set(card.bindingKey(binding), '{}');
assert.throws(() => card.loadCardPurchase(storage, binding), /reconcile/i);
assert.throws(() => card.startCardPurchase(binding, 0n, binding.expiresAt), /Expired/);
assert.throws(() => card.startCardPurchase({ ...binding, quote: { ...binding.quote, symbol: 'SUI' } }, 0n), /Invalid/);
assert.equal(card.cardTestMode('testnet', '4c78adac', 'true'), true);
for (const args of [['mainnet', '35834a8a', 'true'], ['localnet', '4c78adac', 'true'], ['testnet', '35834a8a', 'true'], ['testnet', '4c78adac', undefined]]) assert.equal(card.cardTestMode(...args), false);
(async () => {
  const client = manifestClient(m); const seen = [];
  client.getBalance = async req => { seen.push(req); return { balance: { balance: req.coinType === binding.coinType ? '49000000' : '10000000' } }; };
  assert.deepEqual(await card.cardBalances(client, binding), { usdc: 49000000n, sui: 10000000n });
  assert.equal(seen[0].owner, binding.wallet); assert.equal(seen[0].coinType, binding.coinType); assert.equal(seen[1].coinType, '0x2::sui::SUI');
  await assert.rejects(card.cardBalances(client, { ...binding, chain: 'deadbeef' }), /changed/);
  client.getBalance = async () => { throw Error('node unavailable'); };
  await assert.rejects(card.cardBalances(client, binding), /node unavailable/);
  console.log('PASS: subscription card binding/storage, partial/late/abandoned/expired arrivals, negative balance delta, SUI gas, uncertain submissions, fail-closed test mode and verified-chain balance reads');
})().catch(e => { console.error(e); process.exitCode = 1; });
