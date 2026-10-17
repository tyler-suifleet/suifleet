// Verify log batches captured from the real daemon (scripts/test_logs.py)
// with Mysten's SDK: each signature is a personal-message signature by the
// device key over the exact batch bytes, and each batch names that key.
//   node scripts/test_logs_verify.cjs ENVELOPES.json SIGNER
const fs = require("node:fs"), assert = require("node:assert/strict");
const { appRequire } = require("./frontend_test_utils.cjs");
const { isValidPersonalMessageSignature } = appRequire("@mysten/sui/verify");
const { fromBase64, normalizeSuiAddress } = appRequire("@mysten/sui/utils");

(async () => {
  const [file, signer] = process.argv.slice(2);
  const envelopes = JSON.parse(fs.readFileSync(file));
  assert.ok(envelopes.length > 0, "no envelopes captured");
  for (const envelope of envelopes) {
    const bytes = fromBase64(envelope.message);
    assert.ok(await isValidPersonalMessageSignature(bytes, envelope.signature, { address: signer }), "a batch signature does not verify");
    const batch = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    assert.equal(batch.kind, "logs");
    assert.equal(normalizeSuiAddress(batch.signer), normalizeSuiAddress(signer));
    // A changed byte breaks it.
    const tampered = bytes.slice();
    tampered[tampered.length - 2] ^= 1;
    assert.equal(await isValidPersonalMessageSignature(tampered, envelope.signature, { address: signer }).catch(() => false), false);
  }
  console.log(`PASS: ${envelopes.length} captured log batches verified with Mysten's SDK against the device key`);
})().catch((error) => { console.error(error); process.exit(1); });
