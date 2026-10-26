const assert = require('node:assert/strict');
const { loader } = require('./frontend_test_utils.cjs');
const { onboardingConfig, registrationInput, formatSui, displaySui, shortAddress, onboardingProgress } = loader()('lib/onboarding.ts');
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

// Readable balances never round a nonzero amount to zero; exact amounts stay exact.
assert.equal(formatSui('0'), '0 SUI');
assert.equal(formatSui('1'), '0.000000001 SUI');
assert.equal(formatSui('1000000000'), '1 SUI');
assert.equal(formatSui(1234567890123456789n), '1,234,567,890.123456789 SUI');
assert.throws(() => formatSui('-1'), /negative/);
assert.equal(displaySui('0'), '0 SUI');
assert.equal(displaySui('1'), '< 0.0001 SUI');
assert.equal(displaySui('99999'), '< 0.0001 SUI');
assert.equal(displaySui('100000'), '0.0001 SUI');
assert.equal(displaySui('1234567890'), '1.2345 SUI');
assert.equal(shortAddress('0x' + 'ab'.repeat(32)), '0xabab…abab');
assert.equal(shortAddress('0xa'), '0xa');
const ok = (value) => ({ status: 'success', value }), pending = { status: 'pending' }, failed = { status: 'error' };
const states = (...args) => onboardingProgress(...args).map((step) => `${step.state}:${step.status}`);
assert.deepEqual(states(false, pending, pending), ['current:Not connected', 'upcoming:Waiting for sign-in', 'upcoming:Waiting for sign-in', 'upcoming:Needs a registered device']);
assert.deepEqual(states(true, ok('0'), ok(0)), ['done:Connected', 'current:Not funded', 'upcoming:Needs funds first', 'upcoming:Needs a registered device']);
assert.deepEqual(states(true, failed, ok(0)), ['done:Connected', 'blocked:Balance unavailable', 'upcoming:Needs funds first', 'upcoming:Needs a registered device']);
assert.deepEqual(states(true, ok('5'), failed).slice(2), ['blocked:Inventory unavailable', 'upcoming:Needs a registered device']);
assert.deepEqual(states(true, ok('5'), ok(0)).slice(1), ['done:Funds available', 'current:No devices yet', 'upcoming:Needs a registered device']);
// Registration is inventory only: the update step never reports done (no installation/health claim).
assert.deepEqual(states(true, ok('0'), ok(2)).slice(1), ['current:Not funded', 'done:2 registered', 'current:Ready to deploy']);
for (const step of onboardingProgress(true, ok('5'), ok(3))) assert.doesNotMatch(step.status, /online|healthy|installed/i);
console.log('PASS: onboarding readable/exact balances, short addresses, honest connected/funded/registered progress');

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
