const assert = require('node:assert/strict');
const { loader } = require('./frontend_test_utils.cjs');
const { onboardingConfig, registrationInput } = loader()('lib/onboarding.ts');
const key = 'enoki_public_fixture', id = 'fixture.apps.googleusercontent.com';
for (const network of ['localnet', 'devnet', 'unknown']) assert.equal(onboardingConfig(network, key, id).enabled, false);
for (const network of ['testnet', 'mainnet']) {
  assert.equal(onboardingConfig(network, key, id).enabled, true);
  for (const bad of [undefined, '', 'enoki_private_secret']) assert.equal(onboardingConfig(network, bad, id).enabled, false);
  assert.equal(onboardingConfig(network, key, '').enabled, false);
}
const sender = '0x' + '1'.repeat(64), recipient = '0x' + '2'.repeat(64);
assert.throws(() => registrationInput('', recipient, 'aarch64'), /device name/);
assert.throws(() => registrationInput('Pi', '0xb', 'aarch64'), /full device address/);
assert.throws(() => registrationInput('Pi', recipient, 'mips'), /architecture/);
assert.deepEqual(registrationInput(' Pi ', recipient, 'aarch64'), { name: 'Pi', deviceAddress: recipient, arch: 'aarch64' });
console.log('PASS: onboarding network/provider configuration, private-key exclusion, registration inputs');

process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST = require('node:fs').readFileSync(require('node:path').join(__dirname, '../manifests/fixture.localnet.json'), 'utf8');
const { buildRegisterDeviceTx } = loader()('lib/sui.ts');
const register = buildRegisterDeviceTx('Pi', recipient, 'aarch64', sender).getData();
assert.deepEqual(register.commands.map((command) => command.$kind), ['MoveCall', 'MoveCall', 'MoveCall', 'TransferObjects']);
assert.deepEqual(register.commands.slice(0, 3).map((command) => command.MoveCall.function), ['register_device', 'issue_cap', 'activate_device']);
console.log('PASS: registration stays one atomic PTB');

const privateKeyCheck = require('node:child_process').spawnSync(process.execPath, ['--input-type=module', '-e', 'await import("./dapp/next.config.mjs")'], { cwd: require('node:path').resolve(__dirname, '..'), env: { ...process.env, NEXT_PUBLIC_ENOKI_API_KEY: 'enoki_private_fixture' }, encoding: 'utf8' });
assert.notEqual(privateKeyCheck.status, 0);
assert.match(privateKeyCheck.stderr, /must be an Enoki public app key/);
console.log('PASS: frontend build rejects a private Enoki key before bundling');
