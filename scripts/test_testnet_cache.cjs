const assert = require('node:assert/strict');
const { seal, open } = require('./testnet_cache.cjs');
const secret = 'offline-test-only-ci-key';
const plaintext = Buffer.from(JSON.stringify({device_key:'offline-device-secret'}));
const ciphertext = seal(plaintext, secret);
assert.equal(ciphertext.includes(plaintext), false);
assert.equal(ciphertext.includes(Buffer.from(secret)), false);
assert.deepEqual(open(ciphertext, secret), plaintext);
assert.throws(() => open(ciphertext, 'wrong key'));
for (const index of [0, 40, ciphertext.length - 1]) {
  const changed = Buffer.from(ciphertext); changed[index] ^= 1;
  assert.throws(() => open(changed, secret));
}
console.log('authenticated cache: roundtrip, wrong key, header/nonce/data tampering PASS');
