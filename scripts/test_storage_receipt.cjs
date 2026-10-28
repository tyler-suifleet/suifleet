// Execute the read-only repro with offline chain fixtures and real filesystem writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'test_storage_errors_testnet.cjs'), 'utf8');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sf190-receipt-test-'));
async function run(receipt) {
  const manifest = { rpc_url: 'https://offline.invalid', type_origins: { 'fleet_budget::BudgetCreated': '0x1' } };
  let error, output;
  const fakeFs = { ...fs, readFileSync(file, ...args) {
    if (file === '/profile/deployment.json') return JSON.stringify(manifest);
    if (file === '/profile/fleet-storage.json') return JSON.stringify({ packageId: '0x2' });
    return fs.readFileSync(file, ...args);
  } };
  class Client {
    async getChainIdentifier() { return { chainIdentifier: 'offline' }; }
    async listEvents() { return { events: [{ bcs: [] }] }; }
  }
  const helpers = {
    appRequire(name) {
      if (name === '@mysten/sui/grpc') return { SuiGrpcClient: Client };
      return { toHex: () => '4c78adac', fromBase58: () => Buffer.alloc(4) };
    },
    loader: () => name => name === 'lib/fleet-protocol.ts'
      ? { fleet_budget_BudgetCreatedBcs: { parse: () => ({ budget_id: '0x1843fixture' }) } }
      : { fetchStorage: async () => undefined },
  };
  const context = {
    __dirname, Buffer, process: { argv: ['node', 'repro', '/profile', ...(receipt ? [receipt] : [])], env: {} },
    console: { log: text => { output = JSON.parse(text); }, error: e => { error = e; } },
    require(name) {
      if (name.endsWith('/frontend_test_utils.cjs')) return helpers;
      if (name === 'node:fs') return fakeFs;
      if (name === 'node:os') return { tmpdir: () => directory };
      return require(name);
    },
  };
  vm.runInNewContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  return { error, output, exitCode: context.process.exitCode };
}
(async () => {
  try {
    const explicit = path.join(directory, 'explicit.json');
    assert.equal((await run(explicit)).output.writes, 0);
    assert.equal(fs.statSync(explicit).mode & 0o777, 0o600);
    const original = fs.readFileSync(explicit, 'utf8');
    assert.equal((await run(explicit)).error?.code, 'EEXIST');
    assert.equal(fs.readFileSync(explicit, 'utf8'), original);
    const link = path.join(directory, 'symlink.json');
    fs.symlinkSync(explicit, link);
    assert.equal((await run(link)).error?.code, 'EEXIST');
    assert.equal(fs.readFileSync(explicit, 'utf8'), original);
    assert.equal((await run()).output.writes, 0);
    const privateDirectory = fs.readdirSync(directory).find(name => name.startsWith('sf190-testnet-'));
    assert(privateDirectory);
    assert.equal(fs.statSync(path.join(directory, privateDirectory)).mode & 0o777, 0o700);
    assert.equal(fs.statSync(path.join(directory, privateDirectory, 'receipt.json')).mode & 0o777, 0o600);
    console.log('PASS: offline repro receipts are private; existing files and symlinks are rejected without overwrite');
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
})().catch(error => { console.error(error); process.exitCode = 1; });
