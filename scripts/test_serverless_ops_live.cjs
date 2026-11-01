// Invoked by the disposable localnet harness; no indexer or application HTTP server.
const fs = require('node:fs/promises'), path = require('node:path'), assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { appRequire } = require('./frontend_test_utils.cjs');
const { Transaction } = appRequire('@mysten/sui/transactions');
module.exports = async ({ manifest, temporary, operator, group, device, execute, client }) => {
  const manifestFile = path.join(temporary, 'ops-manifest.json');
  await fs.writeFile(manifestFile, JSON.stringify(manifest));
  const run = (command, argument) => JSON.parse(execFileSync(process.execPath,
    [path.resolve(__dirname, 'suifleet_ops.cjs'), manifestFile, command, argument], { encoding: 'utf8', env: { ...process.env, NEXT_PUBLIC_SUIFLEET_TELEMETRY_URL: 'http://127.0.0.1:1' } }));
  assert.equal(run('devices', operator.toSuiAddress()).length, 1);
  assert.equal(run('groups', operator.toSuiAddress()).length, 1);
  const digests = [];
  for (const command of ['remove-devices', 'add-devices', 'create-deployment']) {
    const out = path.join(temporary, command + '-unsigned.json'), request = path.join(temporary, command + '-request.json');
    const intent = command === 'create-deployment' ? { component: 'serverless-cli', version: '1', recipeBlob: 'fixture', artifactSha256: 'a'.repeat(64), recipeSha256: 'b'.repeat(64), targetDevices: [device.objectId] }
      : { group, devices: [device.objectId] };
    await fs.writeFile(request, JSON.stringify({ command, sender: operator.toSuiAddress(), ...intent, out, maxGasMist: command === 'create-deployment' ? 200_000_000 : 50_000_000 }));
    const built = run('build', request);
    assert.equal(built.unsigned, true);
    const transaction = JSON.parse(await fs.readFile(out));
    // Signing/execution belongs to this test wallet, never the product CLI.
    const result = await execute(Transaction.from(Buffer.from(transaction.transaction, 'base64')));
    digests.push(result.digest);
  }
  const deployments = run('deployments', operator.toSuiAddress());
  const deployment = deployments.find(d => d.componentName === 'serverless-cli');
  assert.ok(deployment?.receiptsVerified);
  const { loader } = require('./frontend_test_utils.cjs');
  await require('./receipt_test_helpers.cjs').deleteDeployment(client, execute, deployment.deploymentId, deployment.schemaVersion);
  console.log(JSON.stringify({ check: 'serverless-ops', result: 'PASS', digests, boundary: 'disposable localnet; unsigned CLI builds, external test-wallet signatures and successful effects; no indexer' }));
};
