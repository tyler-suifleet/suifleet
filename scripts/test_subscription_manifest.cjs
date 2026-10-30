const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loader } = require('./frontend_test_utils.cjs');
const { parseManifest } = loader()('lib/deployment-manifest.ts');
const base = JSON.parse(fs.readFileSync('manifests/fixture.localnet.json', 'utf8'));
const usdc = '0xa1ec7fc00a6f40db9693ad1415d0c193ad3906494428cf252621037bd7117e29::usdc::USDC';
const manifest = { ...base, treasury_payee: '0x' + 'ab'.repeat(32), subscription_coins: { USDC: { coin_type: usdc, decimals: 6 } } };
assert.equal(parseManifest(JSON.stringify(manifest)).subscription_coins.USDC.coin_type, usdc);
for (const invalid of [null, {}, { SUI: { coin_type: '0x2::sui::SUI', decimals: 9 } }, { USDC: { coin_type: usdc, decimals: true } }, { USDC: { coin_type: '0x1::fake::USDC', decimals: 6 } }]) {
  assert.throws(() => parseManifest(JSON.stringify({ ...manifest, subscription_coins: invalid })));
}
assert.throws(() => parseManifest(JSON.stringify({ ...manifest, treasury_payee: undefined })));
assert.throws(() => parseManifest(JSON.stringify({ ...manifest, network: 'testnet', chain_identifier: '4c78adac', subscription_coins: { USDC: { coin_type: '0x' + 'ab'.repeat(32) + '::fake::USDC', decimals: 6 } } })));
const previous = process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST;
try {
  process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = JSON.stringify(manifest);
  const { subscriptionCoin } = loader()('lib/deployment-manifest.ts');
  assert.equal(subscriptionCoin('USDC', usdc, manifest.treasury_payee), usdc);
  assert.throws(() => subscriptionCoin('USDC', usdc, '0x' + 'cd'.repeat(32)), /treasury payee mismatch/);
  assert.throws(() => subscriptionCoin('USDC', '0x2::sui::SUI', manifest.treasury_payee), /coin mismatch/);
  assert.throws(() => subscriptionCoin('SUI', '0x2::sui::SUI', manifest.treasury_payee), /coin mismatch/);
} finally {
  if (previous === undefined) delete process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST;
  else process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = previous;
}
console.log('PASS: subscription coin/origin/decimals/payee validation and payment mismatch guards');
