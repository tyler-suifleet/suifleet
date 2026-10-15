// Run only from the trusted main workflow, with an isolated dedicated CI wallet.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRequire } = require('node:module');
const { spawnSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { seal, open } = require('./testnet_cache.cjs');
const req = createRequire(path.resolve('dapp/package.json'));
const expected = '0x99894c1ae83e8179dd05f3f419f3967c24508159fc5ddaf232af67fdf0c2dcac';
async function main() {
  process.umask(0o077);
  const secret = process.env.SUIFLEET_TESTNET_KEY;
  delete process.env.SUIFLEET_TESTNET_KEY;
  if (!secret || process.env.SUIFLEET_TESTNET_ADDRESS !== expected) throw Error('dedicated CI credentials missing/mismatched');
  const { decodeSuiPrivateKey } = await import(pathToFileURL(req.resolve('@mysten/sui/cryptography')));
  const { Ed25519Keypair } = await import(pathToFileURL(req.resolve('@mysten/sui/keypairs/ed25519')));
  const decoded = decodeSuiPrivateKey(secret);
  if (decoded.scheme !== 'ED25519' || Ed25519Keypair.fromSecretKey(decoded.secretKey).toSuiAddress() !== expected) throw Error('wrong CI wallet');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'suifleet-testnet-'));
  const state = path.join(root, 'state.json');
  const cache = path.resolve('encrypted-live-state/fixture.bin');
  let code = 1;
  try {
    if (fs.existsSync(cache)) fs.writeFileSync(state, open(fs.readFileSync(cache), secret), {mode: 0o600});
    const keystore = JSON.stringify([Buffer.concat([Buffer.from([0]), decoded.secretKey]).toString('base64')]);
    const result = spawnSync('python3', ['scripts/ci_testnet_wallet.py', root, state, process.env.BASE_REF],
      {input: keystore, stdio: ['pipe', 'inherit', 'inherit'], env: process.env});
    code = result.status ?? 1;
  } finally {
    try {
      if (fs.existsSync(state)) {
        fs.mkdirSync(path.dirname(cache), {recursive: true, mode: 0o700});
        fs.writeFileSync(cache, seal(fs.readFileSync(state), secret), {mode: 0o600});
      }
    } finally {
      fs.rmSync(root, {recursive: true, force: true});
    }
  }
  process.exitCode = code;
}
main().catch(() => { console.error('CI testnet setup/cache authentication failed'); process.exitCode = 1; });
