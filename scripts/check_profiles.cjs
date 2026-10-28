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

async function checkProfile(dir, clientFactory) {
  try {
    const m = readProfile(dir);
    const client = clientFactory(m);
    await dm.verifyDeployment(client, m);
    return { profile: path.basename(dir), chain: m.chain_identifier, package: m.call_package_id, version: m.package_version,
      registry: m.shared_objects.registry.id, upgrade_cap: m.upgrade_cap_id };
  } catch (error) {
    throw new Error(`${dir}: ${error.message}. After a shared package upgrade, regenerate and commit deployment.json and frontend.env together before rebuilding the console.`);
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
