// Read-only gRPC verification of every committed deployment profile. No wallet.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { loader, appRequire } = require('./frontend_test_utils.cjs');
const dm = loader()('lib/deployment-manifest.ts');

function readProfile(dir) {
  const manifest = dm.parseManifest(fs.readFileSync(path.join(dir, 'deployment.json'), 'utf8'));
  const lines = fs.readFileSync(path.join(dir, 'frontend.env'), 'utf8').split('\n')
    .filter(line => line.startsWith('NEXT_PUBLIC_DEPLOYMENT_MANIFEST='));
  assert.equal(lines.length, 1, 'expected one NEXT_PUBLIC_DEPLOYMENT_MANIFEST');
  const value = lines[0].slice('NEXT_PUBLIC_DEPLOYMENT_MANIFEST='.length);
  const json = value.startsWith("'") && value.endsWith("'") ? value.slice(1, -1) : value;
  assert.deepEqual(JSON.parse(json), manifest, 'frontend.env differs from deployment.json; regenerate both');
  return manifest;
}

// Retry only fetch transport failures, never a failed chain/manifest assertion.
function transportFailure(error) {
  return error instanceof TypeError && error.message === 'fetch failed';
}

async function checkProfile(dir, clientFactory, {
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  warn = message => console.error(message),
} = {}) {
  try {
    const m = readProfile(dir);
    const client = clientFactory(m);
    for (let attempt = 1; ; attempt++) {
      try {
        await dm.verifyDeployment(client, m);
        break;
      } catch (error) {
        if (!transportFailure(error) || attempt >= 3) throw error;
        warn(`${path.basename(dir)}: transport read failed (attempt ${attempt}/3); retrying full verification`);
        await sleep(attempt * 1000);
      }
    }
    return { profile: path.basename(dir), chain: m.chain_identifier, package: m.call_package_id, version: m.package_version,
      registry: m.shared_objects.registry.id, upgrade_cap: m.upgrade_cap_id };
  } catch (error) {
    const guidance = transportFailure(error)
      ? 'Read-only gRPC transport unavailable after 3 attempts; no profile mismatch established.'
      : 'After a shared package upgrade, regenerate and commit deployment.json and frontend.env together before rebuilding the console.';
    throw new Error(`${dir}: ${error.message}. ${guidance}`);
  }
}

async function main() {
  const { SuiGrpcClient } = appRequire('@mysten/sui/grpc');
  const root = path.resolve(__dirname, '../profiles');
  const dirs = fs.readdirSync(root, { withFileTypes: true }).filter(e => e.isDirectory() && fs.existsSync(path.join(root, e.name, 'deployment.json')));
  assert.ok(dirs.length, 'no committed profiles found');
  for (const dir of dirs) console.log(JSON.stringify(await checkProfile(path.join(root, dir.name), m => new SuiGrpcClient({ network: m.network, baseUrl: m.rpc_url }))));
}
if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { readProfile, checkProfile };
