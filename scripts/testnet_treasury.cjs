// Disposable TESTNET keys only. Never use this utility for the company treasury.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { appRequire } = require('./frontend_test_utils.cjs');
const { Ed25519Keypair } = appRequire('@mysten/sui/keypairs/ed25519');
const { MultiSigPublicKey, MultiSigSigner } = appRequire('@mysten/sui/multisig');
const { Transaction } = appRequire('@mysten/sui/transactions');
const { fromBase64 } = appRequire('@mysten/sui/utils');
const { verifyTransactionSignature } = appRequire('@mysten/sui/verify');
const [mode, state] = process.argv.slice(2);
(async () => {
  // Exclusive creation is the existence test. Keep validation and reads on the
  // same descriptor so replacing the pathname cannot substitute unchecked keys.
  if (mode === 'create') {
    let created;
    try {
      created = fs.openSync(state, fs.constants.O_WRONLY | fs.constants.O_CREAT |
        fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
    }
    if (created !== undefined) {
      try {
        fs.writeFileSync(created, JSON.stringify(Array.from({length:3}, () => Ed25519Keypair.generate().getSecretKey())));
      } finally { fs.closeSync(created); }
    }
  }
  const fd = fs.openSync(state, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  let keys;
  try {
    const info = fs.fstatSync(fd);
    assert.ok(info.isFile(), 'test key state must be a regular file');
    assert.equal(info.uid, process.getuid(), 'test key state must belong to this user');
    assert.equal(info.nlink, 1, 'test key state must have one link');
    assert.equal(info.mode & 0o077, 0, 'test key state must be private');
    keys = JSON.parse(fs.readFileSync(fd,'utf8')).map(k => Ed25519Keypair.fromSecretKey(k));
  } finally { fs.closeSync(fd); }
  assert.equal(keys.length,3);
  const multi = MultiSigPublicKey.fromPublicKeys({threshold:2,publicKeys:keys.map(k => ({publicKey:k.getPublicKey(),weight:1}))});
  if (mode === 'create') {
    console.log(JSON.stringify({network:'testnet',chain_identifier:'4c78adac',treasury_payee:multi.toSuiAddress(),threshold:2,public_keys:keys.map(k => ({scheme:'ED25519',public_key:k.getPublicKey().toSuiPublicKey(),weight:1}))}));
  } else if (mode === 'self-test') {
    const bytes = new Uint8Array([1,2,3]);
    assert.throws(() => new MultiSigSigner(multi,keys.slice(0,1)), /./);
    const signed = await new MultiSigSigner(multi,keys.slice(1)).signTransaction(bytes);
    await verifyTransactionSignature(bytes,signed.signature,{address:multi.toSuiAddress()});
    await assert.rejects(verifyTransactionSignature(new Uint8Array([1,2,4]),signed.signature,{address:multi.toSuiAddress()}));
    console.log('PASS: 2-of-3 alternate quorum signs, single signer and modified bytes rejected');
  } else if (mode === 'sign') {
    const bytes = fromBase64(fs.readFileSync(0,'utf8').trim());
    const tx = Transaction.from(bytes).getData();
    assert.equal(tx.sender,multi.toSuiAddress(),'transaction must be sent by this disposable multisig');
    assert.equal(tx.gasData.owner,multi.toSuiAddress(),'gas must belong to this disposable multisig');
    const signed = await new MultiSigSigner(multi,keys.slice(0,2)).signTransaction(bytes);
    await verifyTransactionSignature(bytes,signed.signature,{address:multi.toSuiAddress()});
    console.log(signed.signature);
  } else throw new Error('unknown mode');
})().catch(() => {console.error('Disposable testnet multisig operation failed (private diagnostics suppressed)');process.exit(1);});
